#!/usr/bin/env python3
"""
backfill_ev_fundamentals.py

One-time backfill: populate ev_revenue and ev_ebitda for historical rows in
data/master/financials.parquet using FMP's enterprise-values endpoint.

Rationale: the legacy fundamentals sync nulled these two columns. With the
parquet-primary migration (CHUNK B) the collector now writes EV columns for
new rows, but historical rows remain NULL. This script one-shots the history.

Safe to re-run: idempotent by (ticker, date). Only writes if the computed
value changes or was previously NULL.
"""

import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

from src.data import parquet_store as ps

FMP_KEY      = os.environ['FMP_API_KEY']
FMP_INTERVAL = 0.35   # seconds between calls → ~170/min; FMP 300/min is safer
FINANCIALS   = ROOT / 'data' / 'master' / 'financials.parquet'


def fetch_ev_history(ticker: str, limit: int = 40) -> pd.DataFrame:
    url = (
        f'https://financialmodelingprep.com/stable/enterprise-values'
        f'?symbol={ticker}&period=quarter&limit={limit}&apikey={FMP_KEY}'
    )
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        return pd.DataFrame()
    data = r.json() or []
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if 'date' not in df.columns or 'enterpriseValue' not in df.columns:
        return pd.DataFrame()
    df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.strftime('%Y-%m-%d')
    df = df.rename(columns={'symbol': 'ticker', 'enterpriseValue': 'enterprise_value'})
    return df[['ticker', 'date', 'enterprise_value']].dropna()


def main():
    fin = pd.read_parquet(FINANCIALS)
    print(f'financials.parquet: {len(fin):,} rows, columns: {list(fin.columns)}')
    missing_before = int(fin['ev_revenue'].isna().sum()) + int(fin['ev_ebitda'].isna().sum())
    print(f'NULL counts before — ev_revenue: {fin["ev_revenue"].isna().sum()}, '
          f'ev_ebitda: {fin["ev_ebitda"].isna().sum()}')

    tickers = sorted(fin['ticker'].dropna().unique().tolist())
    print(f'Backfilling EV for {len(tickers)} tickers...')

    # Normalize fin.date to string YYYY-MM-DD for join
    fin['_join_date'] = pd.to_datetime(fin['date'], errors='coerce').dt.strftime('%Y-%m-%d')

    # Accumulate EV per (ticker, date)
    ev_rows = []
    fetched, skipped, errors = 0, 0, 0
    for i, t in enumerate(tickers, 1):
        try:
            df = fetch_ev_history(t)
            if df.empty:
                skipped += 1
            else:
                ev_rows.append(df)
                fetched += 1
            if i % 25 == 0:
                print(f'  {i}/{len(tickers)} tickers processed (fetched={fetched}, skipped={skipped}, errors={errors})')
            time.sleep(FMP_INTERVAL)
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f'  {t}: ERROR {e}')

    if not ev_rows:
        print('No EV data fetched — aborting.')
        return

    ev = pd.concat(ev_rows, ignore_index=True)
    ev = ev.drop_duplicates(subset=['ticker', 'date'], keep='last')
    print(f'\nEV rows fetched: {len(ev):,} across {ev["ticker"].nunique()} tickers')

    # Merge: left-join fin with ev on (ticker, date)
    merged = fin.merge(ev, left_on=['ticker', '_join_date'], right_on=['ticker', 'date'],
                       how='left', suffixes=('', '_ev'))
    if 'date_ev' in merged.columns:
        merged = merged.drop(columns=['date_ev'])

    # Compute ev_revenue and ev_ebitda where enterprise_value is present
    def safe_div(a, b):
        if pd.isna(a) or pd.isna(b) or b == 0:
            return None
        return float(a) / float(b)

    def compute_or_keep(row, col, denom):
        ev_val = row.get('enterprise_value')
        denom_val = row.get(denom)
        if pd.notna(ev_val) and pd.notna(denom_val) and denom_val != 0:
            return round(float(ev_val) / float(denom_val), 4)
        return row.get(col)

    merged['ev_revenue'] = merged.apply(lambda r: compute_or_keep(r, 'ev_revenue', 'revenue'), axis=1)
    merged['ev_ebitda']  = merged.apply(lambda r: compute_or_keep(r, 'ev_ebitda',  'ebitda'),  axis=1)

    out = merged.drop(columns=['_join_date', 'enterprise_value'], errors='ignore')

    # Atomic rewrite via parquet_store
    ps.append_dedup(ps.FUNDAMENTALS_PATH, out, ['ticker', 'period'], mode='replace')

    # Verify
    after = pd.read_parquet(FINANCIALS)
    print(f'\n=== RESULT ===')
    print(f'Total rows: {len(after):,}')
    print(f'ev_revenue non-null: {after["ev_revenue"].notna().sum()} ({after["ev_revenue"].notna().mean()*100:.1f}%)')
    print(f'ev_ebitda  non-null: {after["ev_ebitda"].notna().sum()} ({after["ev_ebitda"].notna().mean()*100:.1f}%)')
    print(f'\nAPI summary — fetched={fetched}, skipped={skipped}, errors={errors}')


if __name__ == '__main__':
    main()
