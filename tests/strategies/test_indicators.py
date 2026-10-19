"""strategies.indicators + BaseStrategy.ind + byte-identical stops."""
import math
import os
import subprocess

import numpy as np
import pandas as pd
import pytest

from strategies import indicators
from strategies import oxford_crabel
from strategies.base import BaseStrategy, REGIME_ATR_SCALE, TARGET_MODE_ENV, target_mode

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _close(n=80, seed=1):
    r = np.random.default_rng(seed)
    return pd.Series(100 + np.cumsum(r.normal(0, 1, n)),
                     index=pd.date_range('2026-01-02', periods=n, freq='B'))


def _ohlc(n=80, seed=2):
    r = np.random.default_rng(seed)
    c = pd.Series(100 + np.cumsum(r.normal(0, 1, n)))
    h = c + r.uniform(0.1, 1.5, n)
    l = c - r.uniform(0.1, 1.5, n)
    return pd.DataFrame({'open': c.shift(1).fillna(c.iloc[0]), 'high': h, 'low': l, 'close': c})


def test_atr_close_diff_equals_old_base_formula():
    c = _close()
    old = c.diff().abs().rolling(14).mean().iloc[-1]
    assert indicators.atr(c, 14, method='close_diff', fallback_pct=None).iloc[-1] == old


def test_atr_true_range_matches_oxford():
    bars = _ohlc()
    assert indicators.atr(bars, 20).iloc[-1] == oxford_crabel.atr(bars, 20)


def test_atr_fallback_and_bad_method():
    c = pd.Series([5.0] * 30)
    out = indicators.atr(c, 14, method='close_diff')
    assert out.iloc[-1] == pytest.approx(0.1) and out.iloc[0] == pytest.approx(0.1)
    assert indicators.atr(c, 14, method='close_diff', fallback_pct=None).iloc[-1] == 0.0
    assert np.isnan(indicators.atr(c.iloc[:5], 14, method='close_diff', fallback_pct=None)).all()
    with pytest.raises(ValueError):
        indicators.atr(c, 14, method='bogus')


def test_realized_vol_reproduces_engine_hv20():
    c = _close()
    pd.testing.assert_series_equal(
        indicators.realized_vol(c, 20, log=False), c.pct_change().rolling(20).std() * 252 ** 0.5)


def test_realized_vol_reproduces_backtest_hv21():
    c = _close()
    pd.testing.assert_series_equal(
        indicators.realized_vol(c, 21, log=True), np.log(c).diff().rolling(21).std() * math.sqrt(252))
    pd.testing.assert_series_equal(
        indicators.realized_vol(c, 21, log=True, annualize=None), np.log(c).diff().rolling(21).std())


def test_zscore_min_periods_and_zero_std_is_nan():
    s = pd.Series([1., 2., 3., 4., 5.])
    z = indicators.zscore(s, 3)
    assert z.iloc[:2].isna().all() and z.iloc[2] == pytest.approx(1.0)
    z2 = indicators.zscore(s, 3, min_periods=2)
    assert np.isnan(z2.iloc[0]) and z2.iloc[1] == pytest.approx(0.7071067811865475)
    flat = indicators.zscore(pd.Series([2.0] * 6), 3)
    assert flat.isna().all()


def test_ema_matches_oxford():
    s = _close()
    assert indicators.ema(s, 12).iloc[-1] == oxford_crabel.ema(s, 12)


def test_rsi_matches_oxford_wilder():
    s = _close(120, seed=5)
    assert abs(indicators.rsi(s, 14).iloc[-1] - oxford_crabel.rsi_wilder(s, 14)) < 1e-12
    up = pd.Series(np.arange(1.0, 40.0))
    assert indicators.rsi(up, 14).iloc[-1] == 100.0


def test_diff_ignore_negative():
    s = pd.Series([1., 3., 2., 5.])
    assert indicators.diff(s).tolist()[1:] == [2., -1., 3.]
    d = indicators.diff(s, ignore_negative=True)
    assert np.isnan(d.iloc[0]) and np.isnan(d.iloc[2]) and d.iloc[1] == 2. and d.iloc[3] == 3.


def test_csum_mavg_sma():
    s = pd.Series([1., 2., 3., 4.])
    assert indicators.csum(s).tolist() == [1., 3., 6., 10.]
    pd.testing.assert_series_equal(indicators.mavg(s, 2), s.rolling(2).mean())
    pd.testing.assert_series_equal(indicators.sma(s, 2), s.rolling(2).mean())


def test_short_and_empty_inputs_return_nan_series():
    for s in (pd.Series([], dtype=float), pd.Series([1., 2., 3.])):
        outs = [
            indicators.atr(s, 14, method='close_diff', fallback_pct=None),
            indicators.atr(pd.DataFrame({'high': s, 'low': s, 'close': s}), 14, fallback_pct=None),
            indicators.realized_vol(s, 20), indicators.realized_vol(s, 20, log=False),
            indicators.zscore(s, 20), indicators.mavg(s, 20), indicators.sma(s, 20),
            indicators.ema(s, 20), indicators.csum(s), indicators.diff(s),
            indicators.rsi(s, 14),
        ]
        for o in outs:
            assert isinstance(o, pd.Series) and o.index.equals(s.index)
        for o in outs[:7]:
            assert o.isna().all() or len(s) == 0


def test_base_exposes_ind():
    assert BaseStrategy.ind is indicators

    class _S(BaseStrategy):
        id = 'T'
        def generate_signals(self, *a, **k):  # pragma: no cover
            return []
    assert hasattr(_S().ind, 'sma')


def test_base_importable_as_top_level_module():
    code = ("import sys; sys.path[:0]=['%s/src/strategies']; import base; "
            "print(base.BaseStrategy.ind.sma.__name__)" % ROOT)
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    r = subprocess.run(['python3', '-c', code], cwd='/', env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == 'sma'


def _oracle(prices_series, direction, current_price, bull_target, bear_target,
            atr_multiplier, regime_state, mode, r_mult=(2.0, 4.0, 8.0)):
    """Inline copy of the pre-change compute_stops_and_targets body."""
    diff = prices_series.diff().abs()
    _atr_raw = diff.rolling(14).mean().iloc[-1] if len(diff) >= 14 else float('nan')
    atr = float(_atr_raw) if (pd.notna(_atr_raw) and float(_atr_raw) > 0) else current_price * 0.02
    effective_atr_mult = atr_multiplier * REGIME_ATR_SCALE.get(regime_state, 1.0)
    d = atr * effective_atr_mult
    if mode == 'flat':
        if direction == 'LONG':
            stop = current_price - d
            t1 = current_price * 1.05
            t2 = current_price * 1.10
            t3 = bull_target or current_price * 1.20
        else:
            stop = current_price + d
            t1 = current_price * 0.95
            t2 = current_price * 0.90
            t3 = bear_target or current_price * 0.80
    else:
        r1, r2, r3 = r_mult
        if direction == 'LONG':
            stop = current_price - d
            t1 = current_price + r1 * d
            t2 = current_price + r2 * d
            t3 = bull_target or current_price + r3 * d
        else:
            stop = current_price + d
            t1 = current_price - r1 * d
            t2 = current_price - r2 * d
            t3 = bear_target or current_price - r3 * d
    return {'stop': round(stop, 4), 't1': round(t1, 4), 't2': round(t2, 4), 't3': round(t3, 4)}


def test_compute_stops_and_targets_byte_identical(monkeypatch):
    class _S(BaseStrategy):
        id = 'T'
        def generate_signals(self, *a, **k):  # pragma: no cover
            return []
    strat = _S()
    rng = np.random.default_rng(12345)
    regimes = list(REGIME_ATR_SCALE) + ['UNKNOWN']
    lengths = [0, 1, 13, 14, 15, 40]
    for i in range(300):
        n = lengths[i % len(lengths)]
        kind = rng.integers(0, 3)
        vals = 100 + np.cumsum(rng.normal(0, 1.5, n)) if n else np.array([])
        if kind == 1:
            vals = np.full(n, 50.0)
        s = pd.Series(vals, dtype=float)
        if kind == 2 and n > 3:
            s.iloc[int(rng.integers(1, n - 1))] = np.nan
        cur = float(rng.uniform(20, 200))
        direction = 'LONG' if rng.integers(0, 2) else 'SHORT'
        bull = float(rng.uniform(100, 300)) if rng.integers(0, 3) == 0 else None
        bear = float(rng.uniform(10, 90)) if rng.integers(0, 3) == 0 else None
        mult = float(rng.choice([1.0, 2.0, 3.5]))
        reg = regimes[int(rng.integers(0, len(regimes)))]
        mode = 'flat' if rng.integers(0, 2) else 'atr_r'
        monkeypatch.setenv(TARGET_MODE_ENV, mode)
        got = strat.compute_stops_and_targets(s, direction, cur, bull, bear, mult, reg)
        want = _oracle(s, direction, cur, bull, bear, mult, reg, mode, strat.target_r_multiples)
        assert got == want, (i, n, kind, mode)
