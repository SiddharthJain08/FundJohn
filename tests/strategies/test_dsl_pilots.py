"""C7c: the three SIGNAL-DSL pilots reproduce their parents' signal dates on
synthetic panels (parity), are registered/sidecar'd, and lint clean."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from strategies import registry, strategy_lint
from strategies.implementations import (
    S_amihud_illiquidity_premium as amihud_orig,
    S_amihud_illiquidity_premium_dsl as amihud_dsl,
    S_golden_cross_trend_signal_tv2 as gc_orig,
    S_golden_cross_trend_signal_tv2_dsl as gc_dsl,
    oxf_bollinger_momentum as boll_orig,
    oxf_bollinger_momentum_dsl as boll_dsl,
)
from strategies.oxford_crabel import OXFORD_ETF_BASKET

IMPL = Path(__file__).resolve().parents[2] / 'src' / 'strategies' / 'implementations'
REG = {'state': 'LOW_VOL'}
PILOTS = {
    'S_golden_cross_trend_signal_tv2_dsl': ('GoldenCrossTrendStateTV2Dsl', 'S_golden_cross_trend_signal'),
    'S_amihud_illiquidity_premium_dsl': ('AmihudIlliquidityPremiumDsl', 'S_amihud_illiquidity_premium'),
    'oxf_bollinger_momentum_dsl': ('OxfBollingerMomentumDsl', 'oxf_bollinger_momentum'),
}


def _gbm(n_rows, tickers, seed, jumps=False):
    """Seeded GBM with per-ticker piecewise drift regimes (crosses/breakouts occur)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range('2022-01-03', periods=n_rows)
    cols = {}
    for t in tickers:
        seg = rng.integers(60, 140)
        drift = np.repeat(rng.normal(0, 0.004, n_rows // seg + 2), seg)[:n_rows]
        r = drift + rng.normal(0, 0.01, n_rows)
        if jumps:
            hit = rng.random(n_rows) < 0.03
            r = r + hit * rng.choice([-0.06, 0.06], n_rows)
        cols[t] = 50.0 * np.exp(np.cumsum(r))
    return pd.DataFrame(cols, index=idx)


def _sigset(strat, prices, universe, start, regime=REG):
    out, per_date = set(), {}
    for i in range(start, len(prices)):
        sl = prices.iloc[:i + 1]
        sigs = strat.generate_signals(sl, dict(regime), universe)
        s = {(sl.index[-1], x.ticker, x.direction) for x in sigs}
        per_date[sl.index[-1]] = s
        out |= s
    return out, per_date


def test_golden_cross_parity():
    prices = _gbm(450, [f'ZZTEST{i}' for i in range(1, 13)], seed=1)
    u = list(prices.columns)
    a, pa = _sigset(gc_orig.GoldenCrossTrendStateTV2(), prices, u, 200)
    b, pb = _sigset(gc_dsl.GoldenCrossTrendStateTV2Dsl(), prices, u, 200)
    assert pa == pb
    assert a, 'no signals at all'
    dates = sorted(pa)
    crosses = [d for p, d in zip(dates, dates[1:])
               if {t for _, t, _ in pa[d]} - {t for _, t, _ in pa[p]}]
    assert crosses, 'no cross date occurred'


def test_amihud_parity(monkeypatch):
    names = [f'ZZTEST{i}' for i in range(1, 41)]
    prices = _gbm(300, names, seed=2)
    rng = np.random.default_rng(3)
    vol = pd.DataFrame(rng.uniform(1e5, 5e6, prices.shape) * rng.uniform(0.2, 5, len(names)),
                       index=prices.index, columns=names)
    for mod in (amihud_orig, amihud_dsl):
        monkeypatch.setattr(mod, 'load_wide', lambda field, tickers, _v=vol: _v[[t for t in tickers if t in _v.columns]])
        monkeypatch.setattr(mod, 'liquid_pool', lambda p, **kw: list(names))
    a, pa = _sigset(amihud_orig.AmihudIlliquidityPremium(), prices, names, 0)
    b, pb = _sigset(amihud_dsl.AmihudIlliquidityPremiumDsl(), prices, names, 0)
    assert pa == pb
    assert any(d == 'LONG' for _, _, d in a) and any(d == 'SHORT' for _, _, d in a)
    assert all(not s for d, s in pa.items() if d.month == (d - pd.offsets.BDay(1)).month)


def test_bollinger_parity():
    tick = list(OXFORD_ETF_BASKET[:10])
    close = _gbm(260, tick, seed=4, jumps=True)
    ohlc = {t: pd.DataFrame({'open': close[t], 'high': close[t], 'low': close[t], 'close': close[t]})
            for t in tick}
    o, d = boll_orig.OxfBollingerMomentum(), boll_dsl.OxfBollingerMomentumDsl()
    o._ohlc_cache, d._ohlc_cache = dict(ohlc), dict(ohlc)
    a, pa = _sigset(o, close, tick, 100)
    b, pb = _sigset(d, close, tick, 100)
    assert pa == pb
    assert {x for _, _, x in a} == {'LONG', 'SHORT'}


def test_bollinger_threshold_derivation():
    assert boll_dsl.OxfBollingerMomentumDsl.SIGNAL_THRESHOLD == (-2.0 * math.sqrt(79 / 80), 2.0 * math.sqrt(79 / 80))


@pytest.mark.parametrize('sid', sorted(PILOTS))
def test_pilot_ids_match_stems_and_registry(sid):
    import importlib
    cls_name = PILOTS[sid][0]
    mod = importlib.import_module(f'strategies.implementations.{sid}')
    assert getattr(mod, cls_name).id == sid
    assert registry._IMPL_MAP[sid] == (f'strategies.implementations.{sid}', cls_name)


@pytest.mark.parametrize('sid', sorted(PILOTS))
def test_pilot_requirements_mirror_parents(sid):
    new = json.loads((IMPL / f'{sid}.requirements.json').read_text())
    par = json.loads((IMPL / f'{PILOTS[sid][1]}.requirements.json').read_text())
    assert new['strategy_id'] == sid
    assert new['required'] == par['required'] and new['optional'] == par['optional']


@pytest.mark.parametrize('sid', sorted(PILOTS))
def test_pilots_lint_clean(sid):
    assert strategy_lint.lint_file(str(IMPL / f'{sid}.py')) == []
