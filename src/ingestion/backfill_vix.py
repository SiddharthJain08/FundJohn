#!/usr/bin/env python3
"""
backfill_vix.py — one-shot backfill of VIX / VIX3M / VVIX history from yfinance.

Live macro.parquet only carries data from 2021-04-19 onward (the daily
collector's 5-year window). The regime-stratified backtest needs deeper
history — specifically through Feb–May 2020 to exercise the CRISIS
regime — so this script extends VIX coverage back to 2016-04-10
(matching prices.parquet's earliest date).

Idempotent: append_dedup(MACRO_PATH, ..., key=['date','series']) is the
canonical writer; rows already present are overwritten with identical
values so re-runs are no-ops.

Usage
-----
    python3 src/ingestion/backfill_vix.py                  # default 2016-04-10 → 2021-04-18
    python3 src/ingestion/backfill_vix.py --from 2010-01-01 --to 2021-04-18
    python3 src/ingestion/backfill_vix.py --dry-run        # fetch only, don't write
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.data import parquet_store as ps  # noqa: E402

# Default backfill range: prices.parquet starts 2016-04-10; live macro starts 2021-04-19.
DEFAULT_FROM = '2016-04-10'
DEFAULT_TO   = '2021-04-18'

VOL_INDICES = [
    ('^VIX',   'VIX'),
    ('^VIX3M', 'VIX3M'),
    ('^VVIX',  'VVIX'),
]


def _fetch_series(ticker: str, start: str, end_inclusive: str) -> pd.DataFrame:
    """yfinance treats `end` as exclusive; bump by one day so the user-given
    end date is included in the result."""
    end_excl = (date.fromisoformat(end_inclusive) + timedelta(days=1)).isoformat()
    df = yf.download(ticker, start=start, end=end_excl,
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame(columns=['date', 'value'])
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[['Close']].rename(columns={'Close': 'value'})
    df.index.name = 'date'
    df = df.reset_index()
    df['date'] = pd.to_datetime(df['date']).dt.date
    df = df.dropna(subset=['value'])
    return df[['date', 'value']]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='start', default=DEFAULT_FROM)
    ap.add_argument('--to',   dest='end',   default=DEFAULT_TO)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    summary: dict[str, int] = {}
    all_rows: list[dict] = []

    for yf_ticker, series_name in VOL_INDICES:
        try:
            df = _fetch_series(yf_ticker, args.start, args.end)
            if df.empty:
                print(f'  {series_name}: 0 rows fetched ({args.start} → {args.end})',
                      file=sys.stderr)
                summary[series_name] = 0
                continue
            for _, row in df.iterrows():
                all_rows.append({
                    'date':   row['date'],
                    'series': series_name,
                    'value':  float(row['value']),
                    'source': 'yfinance',
                })
            summary[series_name] = len(df)
            print(f'  {series_name}: +{len(df)} rows '
                  f'({df["date"].min()} → {df["date"].max()})', file=sys.stderr)
        except Exception as e:
            print(f'  {series_name}: ERROR — {e}', file=sys.stderr)
            summary[series_name] = -1

    if args.dry_run:
        print(json.dumps({'status': 'dry_run', 'fetched': summary,
                          'total_rows': len(all_rows)}))
        return 0

    if all_rows:
        total = ps.write_macro(all_rows)
        print(f'  macro.parquet rows after write: {total}', file=sys.stderr)
    print(json.dumps({'status': 'ok', 'inserted': summary,
                      'total_rows_written': len(all_rows)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
