"""_load_options_window ≡ the old read path, in ~1/3 the memory.

2026-08-06 second-wave OOM fix: the options_eod window read materialized
~16M rows of object-dtype strings (5.2GB peak, rc=137 class). The new loader
dictionary-encodes at the arrow layer and drops zero-greek rows before
pandas. These tests pin output equivalence against the exact old pipeline
(_read_parquet_window → _drop_zero_greeks → row-wise to_datetime) on a
fixture that exercises zero-greek rows, null greeks, malformed expiries, and
multi-date chains.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import engine  # noqa: E402

TODAY = pd.Timestamp('2026-08-06')


@pytest.fixture
def opts_parquet(tmp_path):
    rng = np.random.default_rng(7)
    n = 400
    dates = np.repeat(['2026-07-28', '2026-07-30', '2026-08-04', '2026-08-05'], n // 4)
    df = pd.DataFrame({
        'ticker':             rng.choice(['AAPL', 'SPY', 'GLD', 'TSLA'], n),
        'date':               dates,
        'expiry':             rng.choice(['2026-08-15', '2026-08-21', '2026-09-18',
                                          '2027-01-15'], n),
        'strike':             rng.choice([90.0, 100.0, 110.0, 120.0], n),
        'option_type':        rng.choice(['call', 'put'], n),
        'implied_volatility': rng.uniform(0.1, 0.9, n),
        'delta':              rng.uniform(-1, 1, n),
        'gamma':              rng.uniform(0, 0.2, n),
        'theta':              rng.uniform(-0.1, 0, n),
        'vega':               rng.uniform(0, 0.5, n),
        'open_interest':      rng.choice([np.nan, 0.0, 150.0], n),
        'volume':             rng.uniform(0, 5000, n),
        'open':               rng.uniform(1, 20, n),
        'close':              rng.uniform(1, 20, n),
    })
    # Degenerate rows: all four greeks zero (some via NaN→0 semantics).
    df.loc[:20, ['delta', 'gamma', 'theta', 'vega']] = 0.0
    df.loc[21:30, ['delta', 'gamma', 'theta', 'vega']] = np.nan
    # A malformed expiry the old path coerces to NaT.
    df.loc[35, 'expiry'] = 'not-a-date'
    # Rows outside the window that the date filter must exclude.
    df.loc[36:40, 'date'] = '2026-06-01'
    p = tmp_path / 'options_eod.parquet'
    df.to_parquet(p, index=False)
    return p


def _old_path(path, window_days):
    """The pre-2026-08-06 pipeline PLUS the in_band cut that
    _inject_intraday_options applied on overlay days — i.e. the semantics of
    record (15:00 ET same-day compute), which the loader now applies always."""
    df = engine._read_parquet_window(path, engine._OPTIONS_SIGNAL_COLS,
                                     window_days, TODAY)
    df = engine._drop_zero_greeks(df)
    df['expiry'] = pd.to_datetime(df['expiry'], errors='coerce')
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    dte_today = (df['expiry'] - TODAY).dt.days
    dte_own = (df['expiry'] - df['date']).dt.days
    from ingestion.intraday_options import MAX_DTE
    df = df[(dte_today > 0) & (dte_own <= MAX_DTE)]
    return df.reset_index(drop=True)


def _normalize(df):
    out = df.copy()
    for c in ('ticker', 'option_type'):
        out[c] = out[c].astype(object)
    return out.reset_index(drop=True)


def test_loader_matches_old_path(opts_parquet):
    old = _old_path(opts_parquet, 14)
    new = engine._load_options_window(opts_parquet, engine._OPTIONS_SIGNAL_COLS,
                                      14, TODAY)
    assert isinstance(new['ticker'].dtype, pd.CategoricalDtype)
    assert isinstance(new['option_type'].dtype, pd.CategoricalDtype)
    assert str(new['date'].dtype).startswith('datetime64')
    assert str(new['expiry'].dtype).startswith('datetime64')
    pd.testing.assert_frame_equal(
        _normalize(old), _normalize(new), check_dtype=False)


def test_loader_aggregation_equivalence(opts_parquet):
    """The downstream per-ticker math (groupby, .str.upper(), expiry
    arithmetic, daily IV means) is value-identical on the category frame."""
    old = _old_path(opts_parquet, 14)
    new = engine._load_options_window(opts_parquet, engine._OPTIONS_SIGNAL_COLS,
                                      14, TODAY)

    def derive(df):
        out = {}
        for tk, grp in df.groupby('ticker', observed=True):
            future = grp[grp['expiry'] >= TODAY].copy()
            if future.empty:
                continue
            future['dte'] = (future['expiry'] - TODAY).dt.days
            near = future[future['dte'] <= 45]
            if near.empty:
                near = future
            chain = near[near['expiry'] == near['expiry'].min()]
            calls = chain[chain['option_type'].str.upper() == 'CALL']
            out[tk] = (
                round(float(grp.groupby('date')['implied_volatility'].mean().sum()), 10),
                round(float(chain['volume'].fillna(0).sum()), 6),
                round(float(calls['implied_volatility'].mean()), 10)
                if not calls.empty else None,
                sorted(chain['strike'].unique().tolist()),
            )
        return out

    assert derive(old) == derive(new)


def test_loader_dropped_all_degenerate(opts_parquet):
    new = engine._load_options_window(opts_parquet, engine._OPTIONS_SIGNAL_COLS,
                                      14, TODAY)
    greeks = new[['delta', 'gamma', 'theta', 'vega']].fillna(0)
    assert not (greeks == 0).all(axis=1).any(), \
        'all-zero-greek rows must be dropped at the arrow layer'


def test_loader_applies_dte_band(opts_parquet):
    """Expired contracts and beyond-MAX_DTE LEAPS never reach the frame —
    the band applies on EVERY run now, not only when an overlay exists."""
    from ingestion.intraday_options import MAX_DTE
    new = engine._load_options_window(opts_parquet, engine._OPTIONS_SIGNAL_COLS,
                                      14, TODAY)
    assert (new['expiry'] > TODAY).all()
    dte_own = (new['expiry'] - new['date']).dt.days
    assert (dte_own <= MAX_DTE).all()
    # The fixture's 2027-01-15 LEAPS (dte_own ~162-171) must be gone.
    assert not (new['expiry'] == pd.Timestamp('2027-01-15')).any()
