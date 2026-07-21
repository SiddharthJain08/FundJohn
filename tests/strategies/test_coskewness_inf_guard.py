"""Regression for ERR-20260721-001.

`S_conditional_coskewness_factor` must never feed non-finite values to
`np.linalg.lstsq`. A zero / near-zero prior close makes `pct_change() == inf`,
which historically flowed into LAPACK gelsd -> DLASCL "parameter number 4 had an
illegal value" — a process-aborting failure, NOT a catchable Python exception,
so the old per-ticker try/except did not save it. The fix filters with
`np.isfinite` (not `~np.isnan`, which leaks inf) on both the market design rows
and each ticker's returns.
"""
import numpy as np
import pandas as pd
from strategies.implementations.S_conditional_coskewness_factor import ConditionalCoskewnessFactor

CRISIS = {'state': 'CRISIS'}  # a regime the strategy is active in


def _panel(n_days=400, n_tickers=30, seed=7):
    """Deterministic geometric-random-walk panel: SPY (market proxy) + N tickers,
    long enough to clear MIN_OBS (252) and yield >= 20 coskew estimates."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range('2021-01-04', periods=n_days)
    cols = {}
    for name in ['SPY'] + [f'T{i:02d}' for i in range(n_tickers)]:
        steps = rng.normal(0.0004, 0.012, n_days)
        cols[name] = 100.0 * np.exp(np.cumsum(steps))
    return pd.DataFrame(cols, index=idx)


def test_mask_semantics_isfinite_blocks_inf_that_isnan_leaks():
    # The crux of the fix: the OLD mask (~isnan) lets +/-inf through; the NEW mask
    # (isfinite) drops it. inf is exactly what triggered DLASCL.
    arr = np.array([1.0, np.inf, -np.inf, np.nan])
    assert list(~np.isnan(arr)) == [True, True, True, False]      # old: infs LEAK
    assert list(np.isfinite(arr)) == [True, False, False, False]  # new: infs blocked


def test_clean_panel_produces_signals():
    s = ConditionalCoskewnessFactor()
    p = _panel()
    sigs = s.generate_signals(p, CRISIS, list(p.columns))
    assert isinstance(sigs, list) and len(sigs) > 0


def test_ticker_inf_return_survives_and_still_signals():
    s = ConditionalCoskewnessFactor()
    p = _panel()
    p.iloc[200, p.columns.get_loc('T05')] = 0.0   # zero prior close -> inf return next day
    sigs = s.generate_signals(p, CRISIS, list(p.columns))
    assert isinstance(sigs, list) and len(sigs) > 0  # no crash, still trades


def test_market_proxy_inf_return_survives():
    s = ConditionalCoskewnessFactor()
    p = _panel()
    p.iloc[200, p.columns.get_loc('SPY')] = 0.0   # inf in the market design row
    sigs = s.generate_signals(p, CRISIS, list(p.columns))
    assert isinstance(sigs, list)  # no crash — that row is dropped for every ticker


def test_no_nonfinite_reaches_lstsq(monkeypatch):
    """Definitive guard: with inf-laced inputs, np.linalg.lstsq must only ever be
    handed finite matrices. This assertion FAILS on the pre-fix code."""
    real = np.linalg.lstsq
    seen = {'calls': 0}

    def guarded(a, b, *args, **kwargs):
        seen['calls'] += 1
        assert np.isfinite(a).all(), 'non-finite A reached lstsq'
        assert np.isfinite(b).all(), 'non-finite b reached lstsq'
        return real(a, b, *args, **kwargs)

    monkeypatch.setattr(np.linalg, 'lstsq', guarded)
    s = ConditionalCoskewnessFactor()
    p = _panel()
    p.iloc[200, p.columns.get_loc('T05')] = 0.0   # ticker inf
    p.iloc[150, p.columns.get_loc('SPY')] = 0.0   # market inf
    sigs = s.generate_signals(p, CRISIS, list(p.columns))
    assert seen['calls'] > 0        # the lstsq path was actually exercised
    assert isinstance(sigs, list)
