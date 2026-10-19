"""tests/strategies/test_signal_expr.py — C7a expression compiler (synthetic panels only)."""
from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import indicators as ind  # noqa: E402
from strategies import signal_expr as se  # noqa: E402
from strategies.safe_ops import safe_div  # noqa: E402

TICKS = ['ZZTESTA', 'ZZTESTB', 'ZZTESTC']


def _panel(n=120, seed=0, fields=('close', 'volume'), gaps=False):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range('2024-01-02', periods=n)
    d = {}
    for f in fields:
        base = 100 + rng.normal(0, 1, (n, 3)).cumsum(axis=0) if f != 'volume' else rng.uniform(1e5, 2e5, (n, 3))
        fr = pd.DataFrame(base, index=idx, columns=TICKS)
        if f == 'high':
            fr = fr + 1.0
        if f == 'low':
            fr = fr - 1.0
        d[f] = fr
    if gaps:
        d['close'].iloc[[10, 11, 40], 0] = np.nan
    return se.make_panel(d)


C = {'close', 'volume'}
HLC = {'high', 'low', 'close', 'volume'}


def test_rejects_unknown_symbol():
    with pytest.raises(se.SignalExprError, match="unknown symbol 'vol'"):
        se.validate_expr_syntax('sma(vol, 5)', columns={'close'})


@pytest.mark.parametrize('e', ['exp(close)', 'Integral(close)', 'eval(close)'])
def test_rejects_unknown_function(e):
    with pytest.raises(se.SignalExprError, match='unknown function'):
        se.validate_expr_syntax(e, columns={'close'})


@pytest.mark.parametrize('e', ['close.__class__', 'close[0]', '(lambda: 1)()', "__import__('os')",
                               'sma(close, n=5)', "'a'", 'close > 1', 'close and 1', 'sma(*close)',
                               'True', '1j', 'close if 1 else 2', '[close]', 'close % 2', 'close // 2'])
def test_hostile_strings_never_reach_sympy(monkeypatch, e):
    import sympy.parsing.sympy_parser as sp
    def boom(*a, **k):
        raise AssertionError('reached sympy')
    monkeypatch.setattr(sp, 'parse_expr', boom)
    with pytest.raises(se.SignalExprError):
        se.compile_signal(e, columns={'close'})
    with pytest.raises(se.SignalExprError):
        se.validate_expr_syntax(e, columns={'close'})
    with pytest.raises(se.SignalExprError):
        se.formula_hash(e)


@pytest.mark.parametrize('e', ['lag(close, 0)', 'lag(close, -1)', 'sma(close, 2.5)', 'sma(close, close)',
                               'sma(close, True)', 'sma(close)', 'rank_pct(close, 5)', 'atr(close, 14)',
                               'atr(14)', 'abs(close, 1)'])
def test_rejects_bad_windows_and_arity(e):
    with pytest.raises(se.SignalExprError):
        se.validate_expr_syntax(e, columns={'close'})


def test_window_message():
    with pytest.raises(se.SignalExprError, match='window must be a positive integer literal'):
        se.validate_expr_syntax('lag(close, 0)', columns={'close'})


@pytest.mark.parametrize('cols', [{'class'}, {'a b'}, {'sma'}, {'abs'}, {'1x'}])
def test_rejects_bad_column_names(cols):
    with pytest.raises(se.SignalExprError):
        se.validate_expr_syntax('1', columns=cols)


def test_pow_dos_guards():
    for e in ['close ** 1000', 'close ** (1/2)', '9 ** 9 ** 9', 'close ** (close ** 2)', '1e9 * close']:
        with pytest.raises(se.SignalExprError):
            se.formula_hash(e)


def test_sma_is_per_column_dropna_rolling():
    p = _panel(gaps=True)
    out = se.compile_signal('sma(close, 5)', columns=C)(p)
    for t in TICKS:
        s = p['close'][t]
        pd.testing.assert_series_equal(out[t], s.dropna().rolling(5).mean().reindex(s.index),
                                       check_names=False)
    assert out.columns.tolist() == TICKS


def test_rank_pct_is_cross_sectional():
    p = _panel()
    out = se.compile_signal('rank_pct(close)', columns=C)(p)
    pd.testing.assert_frame_equal(out, p['close'].rank(axis=1, pct=True), check_names=False)


def test_std_is_ddof1():
    p = _panel()
    out = se.compile_signal('std(close, 10)', columns=C)(p)
    np.testing.assert_allclose(out['ZZTESTA'].iloc[-1], p['close']['ZZTESTA'].iloc[-10:].std(ddof=1))


def test_lag_and_pct_change():
    p = _panel()
    c = p['close']
    lag = se.compile_signal('lag(close, 3)', columns=C)(p)
    pd.testing.assert_frame_equal(lag, c.shift(3), check_names=False)
    pc = se.compile_signal('pct_change(close, 2)', columns=C)(p)
    pd.testing.assert_frame_equal(pc, c / c.shift(2) - 1, check_names=False)


def test_division_by_zero_is_nan():
    out = se.compile_signal('close / (close - close)', columns=C)(_panel())
    assert out.isna().all().all()


def test_arithmetic_and_minmax_zscore_rsi():
    p = _panel()
    c = p['close']
    out = se.compile_signal('max(close, 5) - min(close, 5) + abs(-close) * 2 ** 2 - -1', columns=C)(p)
    exp = c.rolling(5).max() - c.rolling(5).min() + c * 4 + 1
    pd.testing.assert_frame_equal(out, exp, check_names=False)
    z = se.compile_signal('zscore(close, 20)', columns=C)(p)
    pd.testing.assert_series_equal(z['ZZTESTB'], ind.zscore(c['ZZTESTB'], 20), check_names=False)
    r = se.compile_signal('rsi(close, 14)', columns=C)(p)
    pd.testing.assert_series_equal(r['ZZTESTC'], ind.rsi(c['ZZTESTC'], 14), check_names=False)


def test_atr_matches_indicator():
    p = _panel(fields=('high', 'low', 'close', 'volume'))
    out = se.compile_signal('atr(14)', columns=HLC)(p)
    t = 'ZZTESTA'
    hlc = pd.DataFrame({'high': p['high'][t], 'low': p['low'][t], 'close': p['close'][t]})
    pd.testing.assert_series_equal(out[t], ind.atr(hlc, 14, method='true_range', fallback_pct=None),
                                   check_names=False)


@pytest.mark.parametrize('e,lb', [
    ('sma(close, 200)', 200),
    ('sma(abs(pct_change(close, 1)) / (close * volume), 60)', 61),
    ('lag(sma(close, 5), 3)', 8),
    ('atr(14)', 15),
    ('rank_pct(close)', 1),
    ('ema(close, 10)', 10),
    ('rsi(close, 14)', 15),
])
def test_required_lookback(e, lb):
    assert se.compile_signal(e, columns=HLC).required_lookback == lb


def test_formula_hash_canonical():
    a = se.formula_hash('close*volume', columns=C)
    assert a == se.formula_hash('volume*close', columns=C)
    assert se.formula_hash('sma(close,20)', columns=C) != se.formula_hash('sma(close,21)', columns=C)
    assert re.fullmatch(r'[0-9a-f]{16}', a)
    for e in ('close*volume', 'sma(close, 20) / close - 1'):
        assert se.formula_hash(e) == se.formula_hash(e, columns=C)
        assert se.formula_hash(e, columns=C) == se.compile_signal(e, columns=C).formula_hash


def test_compiled_attributes():
    cs = se.compile_signal('close * 2', columns=C)
    assert cs.expr == 'close * 2' and cs.columns == frozenset({'close'})


def test_no_tail_slicing_for_ema():
    p = _panel(n=500)
    out = se.compile_signal('ema(close, 20)', columns=C)(p)
    for t in TICKS:
        pd.testing.assert_series_equal(out[t], ind.ema(p['close'][t], 20), check_names=False)


def test_negative_base_fractional_power_is_nan_without_warning():
    p = _panel()
    cs = se.compile_signal('(close - 100) ** 0.5 + (close - 100) ** -2', columns=C)
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        out = cs(p)
    neg = (p['close'] - 100) < 0
    assert out[neg].isna().all().all()
    assert np.isfinite(out.values[~np.isnan(out.values)]).all()
    assert se.compile_signal('close ** -1', columns=C)(p).notna().all().all()


def test_constant_only_and_missing_field():
    with pytest.raises(se.SignalExprError):
        se.compile_signal('1 + 2', columns=C)
    with pytest.raises(se.SignalExprError, match='no field'):
        se.compile_signal('volume', columns=C)(_panel(fields=('close',)))
