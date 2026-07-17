"""Memory-bounded options read for the signals step.

The 2026-06-24 signals-step OOM (rc=137): engine.run_strategies did
`pd.read_parquet(options_eod.parquet)` — a full read of the 6.3M-row file (~GBs in
pandas) that grows daily. The engine only references 14 of 27 columns and looks back
at most 30 days, so the read can be projected + date-windowed via pyarrow predicate
pushdown. These tests pin that bounded read (column projection + trailing window),
behavior-preserving vs a full-read-then-filter for everything within the window.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.execution.engine import _read_parquet_window, _OPTIONS_SIGNAL_COLS


def _synthetic_options(today: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for d_off in (200, 100, 61, 59, 30, 1):     # days before `today`
        ds = (today - pd.Timedelta(days=d_off)).strftime('%Y-%m-%d')
        rows.append({
            'ticker': 'AAPL', 'date': ds, 'expiry': '2026-09-18', 'strike': 200.0,
            'option_type': 'call', 'implied_volatility': 0.30, 'delta': 0.50,
            'gamma': 0.01, 'theta': -0.02, 'vega': 0.10, 'open_interest': 100,
            'volume': 50, 'open': 1.0, 'close': 1.2,
            # extra columns the engine never references — must be pruned:
            'rho': 0.05, 'bid': 1.1, 'ask': 1.3, 'market_price': 1.2, 'data_source': 'alpaca',
        })
    return pd.DataFrame(rows)


def test_signal_cols_are_the_14_referenced_columns():
    assert set(_OPTIONS_SIGNAL_COLS) == {
        'ticker', 'date', 'expiry', 'strike', 'option_type', 'implied_volatility',
        'delta', 'gamma', 'theta', 'vega', 'open_interest', 'volume', 'open', 'close',
    }


def test_read_projects_columns_and_windows_dates(tmp_path):
    today = pd.Timestamp('2026-06-24')
    p = tmp_path / 'options_eod.parquet'
    _synthetic_options(today).to_parquet(p, index=False)

    out = _read_parquet_window(p, _OPTIONS_SIGNAL_COLS, window_days=60, today=today)

    # Column projection — only the 14 referenced columns, extras dropped.
    assert set(out.columns) == set(_OPTIONS_SIGNAL_COLS)
    for extra in ('rho', 'bid', 'ask', 'market_price', 'data_source'):
        assert extra not in out.columns

    # Trailing-window — only rows with date >= today-60d (d_off 59,30,1 → 3 rows).
    assert len(out) == 3
    cutoff = (today - pd.Timedelta(days=60)).strftime('%Y-%m-%d')
    assert out['date'].min() >= cutoff


def test_read_matches_full_read_then_filter_oracle(tmp_path):
    """Within the window, the bounded read is identical to read-all-then-filter."""
    today = pd.Timestamp('2026-06-24')
    p = tmp_path / 'options_eod.parquet'
    _synthetic_options(today).to_parquet(p, index=False)

    out = _read_parquet_window(p, _OPTIONS_SIGNAL_COLS, window_days=60, today=today)

    cutoff = (today - pd.Timedelta(days=60)).strftime('%Y-%m-%d')
    full = pd.read_parquet(p)
    oracle = full[full['date'] >= cutoff][_OPTIONS_SIGNAL_COLS].reset_index(drop=True)

    key = ['ticker', 'date', 'strike', 'option_type']
    a = out.sort_values(key).reset_index(drop=True)
    b = oracle.sort_values(key).reset_index(drop=True)
    pd.testing.assert_frame_equal(a[sorted(a.columns)], b[sorted(b.columns)], check_dtype=False)


def test_windows_cover_their_lookbacks():
    """Each read window must exceed its consumer's longest lookback: options IV-rank
    is 30 days; HV20 needs ~28 trading days but the len(_g)<22 skip wants headroom."""
    from src.execution.engine import _OPTIONS_READ_WINDOW_DAYS, _HV20_PRICES_WINDOW_DAYS
    assert _OPTIONS_READ_WINDOW_DAYS >= 30
    assert _HV20_PRICES_WINDOW_DAYS >= 45   # generous over the ~28 trading-day need
