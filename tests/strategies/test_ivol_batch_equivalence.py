"""S_ivol_mispricing_asymmetry: the vectorized _ivol_batch must be numerically
IDENTICAL to the pre-vectorization per-ticker OLS loop (it replaced a per-ticker
np.linalg.lstsq that blew up to a 240-min timeout on the full ~12.5k universe).
"""
import numpy as np
import pandas as pd
from strategies.implementations.s_ivol_mispricing_asymmetry import IvolMispricingAsymmetry


def _ivol_ref(ret, mkt, smb, hml):
    """The exact pre-vectorization per-ticker computation (reference)."""
    df = pd.concat([ret, mkt, smb, hml], axis=1).dropna()
    if len(df) < 10:
        return np.nan
    X = df.iloc[:, 1:].values
    y = df.iloc[:, 0].values
    Xd = np.column_stack([np.ones(len(X)), X])
    coef, *_ = np.linalg.lstsq(Xd, y, rcond=None)
    resid = y - Xd @ coef
    return float(np.std(resid, ddof=1) * np.sqrt(252))


def test_ivol_batch_matches_per_ticker_reference():
    rng = np.random.default_rng(0)
    T, N = 21, 80
    idx = pd.RangeIndex(T)
    mkt = pd.Series(rng.normal(0, 0.01, T), index=idx)
    smb = pd.Series(0.0, index=idx)          # zero-proxies, as in the strategy
    hml = pd.Series(0.0, index=idx)
    cols = [f'T{i:02d}' for i in range(N)]
    rets = pd.DataFrame(rng.normal(0, 0.02, (T, N)), columns=cols, index=idx)
    rets['T00'] = 0.0                        # edge case: a no-data (all-zero) ticker

    ref = pd.Series({c: _ivol_ref(rets[c], mkt, smb, hml) for c in cols})
    batch = IvolMispricingAsymmetry._ivol_batch(rets, mkt, smb, hml)

    pd.testing.assert_series_equal(
        batch.reindex(cols), ref.reindex(cols),
        check_names=False, rtol=1e-9, atol=1e-12,
    )


def test_ivol_batch_short_window_returns_empty():
    idx = pd.RangeIndex(5)  # < 10 rows
    z = pd.Series(0.0, index=idx)
    rets = pd.DataFrame({'A': [0.01] * 5, 'B': [0.02] * 5}, index=idx)
    assert IvolMispricingAsymmetry._ivol_batch(rets, z, z, z).empty


def test_recent_window_pct_change_matches_full_panel():
    """Memory opt: computing pct_change on only the last LOOKBACK+1 rows must be
    numerically identical to the old full-panel pct_change().iloc[-LOOKBACK:]
    (incl. NaNs) — that's what lets us drop the full-panel copy without changing
    results. Also checks the equal-weight market proxy is slice-invariant."""
    L = 21
    rng = np.random.default_rng(7)
    T, N = 400, 40
    idx = pd.bdate_range('2022-01-03', periods=T)
    closes = pd.DataFrame(
        100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, (T, N)), axis=0)),
        columns=[f'T{i:02d}' for i in range(N)], index=idx,
    )
    closes.iloc[300, 5] = np.nan     # inject a hole → NaN in a couple returns

    full = closes.pct_change().fillna(0).iloc[-L:]
    windowed = closes.iloc[-(L + 1):].pct_change().fillna(0).iloc[-L:]
    pd.testing.assert_frame_equal(windowed, full)

    # equal-weight market proxy: recent mean == full mean, sliced
    pd.testing.assert_series_equal(windowed.mean(axis=1),
                                   closes.pct_change().fillna(0).mean(axis=1).iloc[-L:])


def test_generate_signals_runs_end_to_end_crisis():
    rng = np.random.default_rng(3)
    T, N = 300, 70
    idx = pd.bdate_range('2022-01-03', periods=T)
    cols = [f'T{i:02d}' for i in range(N)]
    prices = pd.DataFrame(
        100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, (T, N)), axis=0)),
        columns=cols, index=idx,
    )
    s = IvolMispricingAsymmetry()
    sigs = s.generate_signals(prices, {'state': 'CRISIS'}, cols)  # eligible regime
    assert isinstance(sigs, list)
