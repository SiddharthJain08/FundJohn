"""
ingest_earnings_calendar.py
===========================
Ingest the forward earnings calendar (next 14 days + trailing 14 for
post-event analysis) to /root/openclaw/data/master/earnings_calendar.parquet.

Strategy that depends on this file
---------------------------------
* S-HV17 Earnings Straddle Fade → next_earnings_date (per ticker,
  within 1-2 trading days) to identify straddles pricing a rich implied
  move relative to historical post-earnings moves.

Schema (long-format):
    ticker (str)
    next_earnings_date (date)
    time (str)           — 'bmo', 'amc', or ''
    eps_estimate (float, optional)
    revenue_estimate (float, optional)
    fiscal_period (str, optional)

Data source
-----------
FMP Starter tier provides `earning_calendar?from=&to=&apikey=`
returning all scheduled earnings in the window.  We keep only the
*next* event per ticker (earliest forward date ≥ today).

Usage
-----
    python ingest_earnings_calendar.py                  # ±14d window
    python ingest_earnings_calendar.py --window 30      # ±30d window
    python ingest_earnings_calendar.py --dry-run

Author: Claude / FundJohn research, 2026-04-23.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path("/root/openclaw/data/master")
OUT_PATH = DATA_DIR / "earnings_calendar.parquet"

FMP_BASE = "https://financialmodelingprep.com/api/v3"


def fmp_earnings_window(api_key: str, from_d: str, to_d: str) -> pd.DataFrame:
    """Pull the full earnings calendar for the window (≤ 90d per FMP limit)."""
    url = f"{FMP_BASE}/earning_calendar"
    params = {'from': from_d, 'to': to_d, 'apikey': api_key}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    js = r.json() or []
    if not js:
        return pd.DataFrame()
    df = pd.DataFrame(js)
    df = df.rename(columns={
        'symbol':        'ticker',
        'date':          'next_earnings_date',
        'epsEstimated':  'eps_estimate',
        'revenueEstimated': 'revenue_estimate',
        'fiscalDateEnding': 'fiscal_period',
    })
    df['next_earnings_date'] = pd.to_datetime(df['next_earnings_date']).dt.date
    keep = ['ticker', 'next_earnings_date', 'time',
            'eps_estimate', 'revenue_estimate', 'fiscal_period']
    return df[[c for c in keep if c in df.columns]].reset_index(drop=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--window', type=int, default=14,
                   help='± days around today to pull (default 14)')
    p.add_argument('--universe-file', default=None,
                   help='Optional txt file with tickers to retain (default = all)')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    api_key = os.environ.get('FMP_API_KEY')
    if not api_key:
        print("ERROR: FMP_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(2)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    from_d = (today - timedelta(days=args.window)).isoformat()
    to_d = (today + timedelta(days=args.window)).isoformat()
    print(f"earnings_calendar ingest — window {from_d} → {to_d}")

    df = fmp_earnings_window(api_key, from_d, to_d)
    print(f"  raw rows        : {len(df):,}")

    if args.universe_file and Path(args.universe_file).exists():
        with open(args.universe_file) as f:
            universe = {ln.strip() for ln in f if ln.strip()
                        and not ln.startswith('#')}
        df = df[df['ticker'].isin(universe)]
        print(f"  universe-filtered : {len(df):,}")

    # For each ticker, keep only the *next* upcoming event ≥ today
    upcoming = df[df['next_earnings_date'] >= today].copy()
    upcoming = upcoming.sort_values(['ticker', 'next_earnings_date'])
    upcoming = upcoming.drop_duplicates(subset=['ticker'], keep='first')

    # And a past-event table so S-HV17 can compute historical-move baselines
    historical = df[df['next_earnings_date'] < today].copy()
    print(f"  upcoming events : {len(upcoming):,}")
    print(f"  trailing events : {len(historical):,}")

    if args.dry_run:
        print("  --dry-run: not writing")
        return

    # The forward table is the operational calendar
    upcoming.to_parquet(OUT_PATH, index=False)
    print(f"  wrote upcoming → {OUT_PATH}")

    # Optional trailing file for post-event analysis
    trail_path = DATA_DIR / "earnings_calendar_trailing.parquet"
    historical.to_parquet(trail_path, index=False)
    print(f"  wrote trailing → {trail_path}")


if __name__ == "__main__":
    main()
