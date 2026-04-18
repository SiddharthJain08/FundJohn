#!/usr/bin/env python3
"""
fetch_vol_indices.py

Fetches VIX and VVIX daily close prices from yfinance and upserts into
the macro_data Postgres table. Called by the daily data pipeline and
on bot startup to backfill history.

Output: JSON with counts to stdout.
"""

import os
import sys
import json
import psycopg2
import pandas as pd
import yfinance as yf
from datetime import date, timedelta

POSTGRES_URI = os.environ['POSTGRES_URI']

# Symbols to fetch: (yfinance_ticker, series_name)
VOL_INDICES = [
    ('^VIX',  'VIX'),
    ('^VVIX', 'VVIX'),
    ('^VIX3M', 'VIX3M'),
]

# How far back to backfill on first run
BACKFILL_DAYS = 365 * 5  # 5 years


def _get_existing_max(cur, series: str) -> str | None:
    cur.execute("SELECT MAX(date)::text FROM macro_data WHERE series = %s", [series])
    row = cur.fetchone()
    return row[0] if row and row[0] else None


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
    conn = psycopg2.connect(POSTGRES_URI)
    conn.autocommit = False
    cur = conn.cursor()

    today = date.today().isoformat()
    results = {}

    for yf_ticker, series_name in VOL_INDICES:
        try:
            max_date = _get_existing_max(cur, series_name)
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

            rows = [(row['date'], series_name, float(row['value']), 'yfinance')
                    for _, row in df.iterrows()]

            cur.executemany(
                """INSERT INTO macro_data (date, series, value, source)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (date, series) DO UPDATE SET value=EXCLUDED.value, source=EXCLUDED.source""",
                rows
            )
            results[series_name] = len(rows)
            print(f'  {series_name}: +{len(rows)} rows ({start} → {today})', file=sys.stderr)

        except Exception as e:
            print(f'  {series_name}: ERROR — {e}', file=sys.stderr)
            results[series_name] = -1

    conn.commit()
    conn.close()
    return results


if __name__ == '__main__':
    out = main()
    print(json.dumps({'status': 'ok', 'inserted': out}))
