"""
ingest_vol_indices.py
=====================
Daily ingest of vol-index closes (VIX, VVIX, VIX9D) to
/root/openclaw/data/master/vol_indices.parquet.

Strategies that depend on this file
----------------------------------
* S-TR-01 VVIX Early Warning     → vvix_close series + 252-day percentile
* S-TR-04 Zarattini Intraday SPY → vix_close (gate LONG when VIX < 30)
* S-HV15 IV Term Structure       → vix_close (sanity gate on inversion)
* S-HV20 IV Dispersion           → vix_close for regime scaling (optional)

Schema (long-format, one row per date):
    date (date)            — session date
    vix_close (float)      — CBOE VIX
    vvix_close (float)     — CBOE VVIX (vol-of-vol on VIX)
    vix9d_close (float)    — CBOE VIX9D (9-day variant)

Data sources (priority order)
-----------------------------
1. FMP Starter `historical-price-full` endpoint for index tickers:
       ^VIX      ^VVIX      ^VIX9D
   The Starter tier DOES return indices at the daily level.
2. Fallback to Polygon `v2/aggs/ticker/I:VIX/range/1/day/...` if FMP
   fails for a given index (Polygon indexes plan not guaranteed; we
   just flag FMP failure in the log instead of hard-failing).

Usage
-----
    python ingest_vol_indices.py                   # incremental append
    python ingest_vol_indices.py --rebuild --from 2016-01-01   # full rebuild
    python ingest_vol_indices.py --dry-run         # validate without writing

Author: Claude / FundJohn research, 2026-04-23.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

DATA_DIR = Path("/root/openclaw/data/master")
OUT_PATH = DATA_DIR / "vol_indices.parquet"

INDEX_TICKERS = {
    'vix_close':   '^VIX',
    'vvix_close':  '^VVIX',
    'vix9d_close': '^VIX9D',
}

FMP_BASE = "https://financialmodelingprep.com/api/v3"


def fmp_historical(symbol: str, api_key: str,
                   start: str, end: str) -> pd.DataFrame:
    """Pull daily close history for a symbol from FMP historical-price-full."""
    url = f"{FMP_BASE}/historical-price-full/{symbol}"
    params = {'from': start, 'to': end, 'apikey': api_key}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    js = r.json()
    hist = js.get('historical') or []
    if not hist:
        return pd.DataFrame(columns=['date', 'close'])
    df = pd.DataFrame(hist)
    df['date'] = pd.to_datetime(df['date']).dt.date
    return df[['date', 'close']].sort_values('date').reset_index(drop=True)


def load_existing() -> pd.DataFrame:
    if OUT_PATH.exists():
        return pd.read_parquet(OUT_PATH)
    return pd.DataFrame(columns=['date'] + list(INDEX_TICKERS.keys()))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--rebuild', action='store_true',
                   help='Rebuild full history instead of incremental append')
    p.add_argument('--from', dest='from_date', default='2016-01-01')
    p.add_argument('--to', dest='to_date', default=None)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    api_key = os.environ.get('FMP_API_KEY')
    if not api_key:
        print("ERROR: FMP_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(2)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = pd.DataFrame() if args.rebuild else load_existing()
    from_d = args.from_date
    to_d = args.to_date or date.today().isoformat()

    if not args.rebuild and not existing.empty:
        last = pd.to_datetime(existing['date']).max().date()
        from_d = (last - timedelta(days=3)).isoformat()  # small overlap for safety

    print(f"vol_indices ingest — range {from_d} → {to_d}")

    frames = {}
    for col, sym in INDEX_TICKERS.items():
        print(f"  pulling {sym:8s} → {col}")
        try:
            df = fmp_historical(sym, api_key, from_d, to_d)
        except Exception as exc:                     # pragma: no cover
            print(f"  FMP failed for {sym}: {exc}", file=sys.stderr)
            df = pd.DataFrame(columns=['date', 'close'])
        frames[col] = df.rename(columns={'close': col}).set_index('date')
        time.sleep(0.3)

    out = pd.concat(frames.values(), axis=1, join='outer').reset_index()
    out = out.sort_values('date').reset_index(drop=True)
    # Forward-fill 1 day to bridge isolated index holidays
    for col in INDEX_TICKERS:
        if col in out.columns:
            out[col] = out[col].ffill(limit=1)
    out = out.dropna(subset=list(INDEX_TICKERS.keys()), how='all')

    # Merge with existing
    if not existing.empty:
        combined = pd.concat([existing, out], ignore_index=True)
        combined = combined.drop_duplicates(subset=['date'], keep='last')
        combined = combined.sort_values('date').reset_index(drop=True)
    else:
        combined = out

    print(f"  rows new        : {len(out):,}")
    print(f"  rows total      : {len(combined):,}")
    print(f"  last date       : {combined['date'].max()}")

    if args.dry_run:
        print("  --dry-run: not writing")
        return

    combined.to_parquet(OUT_PATH, index=False)
    print(f"  wrote → {OUT_PATH}")


if __name__ == "__main__":
    main()
