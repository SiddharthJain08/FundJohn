"""
adjust_for_corporate_actions.py — Phase 2.3 of alpaca-cli integration.

Read-side adjustment for prices.parquet: applies forward-split and
reverse-split ratios retroactively so historical OHLCV behaves as a
continuous price series, instead of carrying the phantom 50% / 90% /
etc. drawdowns that a raw read produces across split events.

The master prices.parquet is NEVER mutated (per the master-data invariant
in CLAUDE.md). Adjustments are applied on read by the backtest caller
that imports `adjusted_close()` or `adjust_dataframe()`. Cash dividends
are recorded but NOT adjusted in this pass — total-return adjustment is
deferred until a downstream backtester explicitly opts in.

Mechanics
---------
For a 10:1 forward split (NVDA on 2024-06-10, new_rate=10, old_rate=1):
  ratio = new_rate / old_rate = 10
  pre-split prices are divided by 10
  pre-split volumes are multiplied by 10

For a 1:5 reverse split (e.g. AAL hypothetically): ratio = 1/5 = 0.2
  pre-split prices ÷ 0.2 = ×5
  pre-split volumes ÷ 5

Multiple splits compose: a 2:1 then a 3:1 → pre-prices ÷ 6.

The corp-actions parquet must have been pulled by
src/pipeline/alpaca_corporate_actions.py first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
CORPORATE_ACTIONS_PARQUET = ROOT / 'data' / 'master' / 'corporate_actions.parquet'


def _load_split_actions_for(ticker: str) -> pd.DataFrame:
    """Return forward + reverse splits for ticker, sorted by ex_date asc.
    Empty DataFrame if no splits or parquet missing."""
    if not CORPORATE_ACTIONS_PARQUET.exists():
        return pd.DataFrame(columns=['symbol', 'action_type', 'ex_date', 'ratio'])
    df = pd.read_parquet(CORPORATE_ACTIONS_PARQUET)
    if df.empty:
        return df
    df = df[(df['symbol'] == ticker) &
            (df['action_type'].isin(['forward_split', 'reverse_split'])) &
            (df['ratio'].notna())]
    if df.empty:
        return df
    df = df.copy()
    df['ex_date'] = pd.to_datetime(df['ex_date']).dt.date
    return df.sort_values('ex_date').reset_index(drop=True)


def adjusted_close(ticker: str, dates, raw_closes) -> list:
    """Return raw_closes with all forward/reverse split adjustments applied
    retroactively. `dates` should be a list/Series of date or datetime values
    aligned 1:1 with raw_closes (close prices).

    For each split with ex_date X:
      raw_closes[i] is divided by the cumulative ratio of all splits whose
      ex_date > date[i]. (A close on or after ex_date is post-split-adjusted
      already.)

    Implementation note: this matches yfinance's "adjusted close" behavior
    for splits only — no dividend adjustment.
    """
    splits = _load_split_actions_for(ticker)
    if splits.empty:
        return list(raw_closes)

    # Normalize dates → Python date objects for comparison
    dates_norm = []
    for d in dates:
        if isinstance(d, str):
            dates_norm.append(pd.to_datetime(d).date())
        elif isinstance(d, pd.Timestamp):
            dates_norm.append(d.date())
        elif hasattr(d, 'date'):
            dates_norm.append(d.date())
        else:
            dates_norm.append(d)

    out = []
    for d, c in zip(dates_norm, raw_closes):
        if c is None or pd.isna(c):
            out.append(c)
            continue
        # Cumulative ratio: every split with ex_date > d divides this price.
        cum_ratio = 1.0
        for _, sp in splits.iterrows():
            if sp['ex_date'] > d:
                cum_ratio *= float(sp['ratio'])
        out.append(float(c) / cum_ratio if cum_ratio != 1.0 else float(c))
    return out


def adjust_dataframe(df: pd.DataFrame, *, ticker_col: str = 'ticker',
                     date_col: str = 'date',
                     close_cols: Iterable[str] = ('open', 'high', 'low', 'close')) -> pd.DataFrame:
    """Return a copy of `df` with split adjustments applied to all
    `close_cols`. Volume is also multiplied by cum_ratio (the inverse of
    the price adjustment) so dollar-volume stays consistent. Caller is
    responsible for passing only the rows for tickers they want adjusted —
    this iterates per-ticker.
    """
    if df.empty:
        return df.copy()
    out = df.copy()
    for ticker in out[ticker_col].unique():
        splits = _load_split_actions_for(ticker)
        if splits.empty:
            continue
        mask = out[ticker_col] == ticker
        sub = out.loc[mask].copy()
        sub_dates = pd.to_datetime(sub[date_col]).dt.date
        cum_ratios = []
        for d in sub_dates:
            cum_ratio = 1.0
            for _, sp in splits.iterrows():
                if sp['ex_date'] > d:
                    cum_ratio *= float(sp['ratio'])
            cum_ratios.append(cum_ratio)
        sub_ratios = pd.Series(cum_ratios, index=sub.index)
        for col in close_cols:
            if col in sub.columns:
                sub[col] = sub[col] / sub_ratios
        if 'volume' in sub.columns:
            sub['volume'] = sub['volume'] * sub_ratios
        out.loc[mask, sub.columns] = sub
    return out
