#!/usr/bin/env python3
"""
fetch_vol_indices.py

Fetches VIX/VVIX/VIX3M daily close prices from yfinance and appends to the
macro.parquet file (parquet-primary storage). Called by the collector and
on bot startup to backfill history.

Output: JSON with counts to stdout.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.data import parquet_store as ps

# Symbols to fetch: (yfinance_ticker, series_name)
VOL_INDICES = [
    ('^VIX',  'VIX'),
    ('^VVIX', 'VVIX'),
    ('^VIX3M', 'VIX3M'),
]

# How far back to backfill on first run
BACKFILL_DAYS = 365 * 5  # 5 years


def _existing_max(series: str) -> str | None:
    """Latest date already in macro.parquet for this series."""
    df = ps.read_filtered(ps.MACRO_PATH, where=f"series='{series}'", cols=['date'],
                          order_by='date DESC', limit=1)
    if df.empty:
        return None
    return pd.to_datetime(df.iloc[0]['date']).date().isoformat()


def _fetch_series(ticker: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[['Close']].rename(columns={'Close': 'value'})
    df.index.name = 'date'
    df = df.reset_index()
    df['date'] = pd.to_datetime(df['date']).dt.date
    df = df.dropna(subset=['value'])
    return df


def main() -> dict:
    today = date.today().isoformat()
    results = {}

    all_rows = []
    for yf_ticker, series_name in VOL_INDICES:
        try:
            max_date = _existing_max(series_name)
            if max_date:
                start = (date.fromisoformat(max_date) + timedelta(days=1)).isoformat()
            else:
                start = (date.today() - timedelta(days=BACKFILL_DAYS)).isoformat()

            if start >= today:
                results[series_name] = 0
                continue

            df = _fetch_series(yf_ticker, start, today)
            if df.empty:
                results[series_name] = 0
                continue

            for _, row in df.iterrows():
                all_rows.append({
                    'date':   row['date'].isoformat() if hasattr(row['date'], 'isoformat') else str(row['date']),
                    'series': series_name,
                    'value':  float(row['value']),
                    'source': 'yfinance',
                })
            results[series_name] = len(df)
            print(f'  {series_name}: +{len(df)} rows ({start} → {today})', file=sys.stderr)

        except Exception as e:
            print(f'  {series_name}: ERROR — {e}', file=sys.stderr)
            results[series_name] = -1

    if all_rows:
        total = ps.write_macro(all_rows)
        print(f'  macro.parquet total rows after write: {total}', file=sys.stderr)

    return results


if __name__ == '__main__':
    out = main()
    print(json.dumps({'status': 'ok', 'inserted': out}))
