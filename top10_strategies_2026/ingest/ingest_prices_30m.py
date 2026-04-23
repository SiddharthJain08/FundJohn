"""
ingest_prices_30m.py
====================
Ingest 30-minute OHLCV bars for the liquid universe to
/root/openclaw/data/master/prices_30m.parquet.

Strategies that depend on this file
----------------------------------
* S-TR-04 Zarattini Intraday SPY   → SPY 30-min bars (first bar + close)
* S-TR-06 Baltussen EOD Reversal   → full liquid universe (30-min bars through 16:00 ET)

Universe
--------
By default we pull bars for the top ~250 US equities by
`avg_dollar_volume_30d` plus the macro / vol ETFs (SPY, QQQ, VXX, UVXY),
matching the universe used by S-TR-06.

Schema (long-format, one row per ticker-bar):
    ticker (str)
    datetime (timestamp, UTC-naïve ET)
    open, high, low, close (float)
    volume (int)
    vwap (float, optional — filled if Polygon returns it)
    transactions (int, optional)

Data source
-----------
Polygon Massive Options Starter includes equities aggregate bars with
`v2/aggs/ticker/{TICKER}/range/30/minute/{from}/{to}` at 5-minute latency.
Unpaid rate-limit tier = 5 calls/sec → wall-clocks at ~50 names/min.  We
throttle to 4/sec to be safe.

Usage
-----
    python ingest_prices_30m.py                    # incremental (yesterday + today)
    python ingest_prices_30m.py --rebuild --from 2020-01-01
    python ingest_prices_30m.py --universe-file my_universe.txt
    python ingest_prices_30m.py --dry-run

Author: Claude / FundJohn research, 2026-04-23.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd
import requests

DATA_DIR = Path("/root/openclaw/data/master")
OUT_PATH = DATA_DIR / "prices_30m.parquet"
UNIVERSE_PATH = DATA_DIR / "universe_top250.txt"

POLYGON_BASE = "https://api.polygon.io"

DEFAULT_UNIVERSE_TAIL = ['SPY', 'QQQ', 'IWM', 'DIA', 'VXX', 'UVXY', 'TQQQ']


def _default_universe() -> List[str]:
    """Fall back to the macro / vol ETFs only if no universe file is present."""
    if UNIVERSE_PATH.exists():
        with open(UNIVERSE_PATH) as f:
            tickers = [ln.strip() for ln in f if ln.strip() and not ln.startswith('#')]
        # Ensure the tail is included
        return list(dict.fromkeys(tickers + DEFAULT_UNIVERSE_TAIL))
    return DEFAULT_UNIVERSE_TAIL


def _polygon_30m_bars(ticker: str, api_key: str,
                      start: str, end: str) -> pd.DataFrame:
    """One Polygon 30-minute aggregate pull.  Returns [] on 4xx/404."""
    url = (f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/30/minute/"
           f"{start}/{end}")
    params = {'adjusted': 'true', 'sort': 'asc', 'limit': 50000, 'apiKey': api_key}
    r = requests.get(url, params=params, timeout=30)
    if r.status_code in (404, 429):
        return pd.DataFrame()
    r.raise_for_status()
    js = r.json()
    if 'results' not in js or not js['results']:
        return pd.DataFrame()
    df = pd.DataFrame(js['results'])
    # Polygon timestamps are ms since epoch UTC.  Convert to ET, drop tz.
    df['datetime'] = (pd.to_datetime(df['t'], unit='ms', utc=True)
                        .dt.tz_convert('US/Eastern').dt.tz_localize(None))
    df = df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low',
                            'c': 'close', 'v': 'volume',
                            'vw': 'vwap', 'n': 'transactions'})
    df['ticker'] = ticker
    # Regular session only: 09:30 – 16:00 ET
    df = df[(df['datetime'].dt.time >= pd.Timestamp('09:30').time()) &
            (df['datetime'].dt.time <= pd.Timestamp('16:00').time())]
    cols = ['ticker', 'datetime', 'open', 'high', 'low', 'close',
            'volume', 'vwap', 'transactions']
    return df[[c for c in cols if c in df.columns]].reset_index(drop=True)


def load_existing() -> pd.DataFrame:
    if OUT_PATH.exists():
        return pd.read_parquet(OUT_PATH)
    return pd.DataFrame(columns=['ticker', 'datetime', 'open', 'high', 'low',
                                 'close', 'volume'])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--rebuild', action='store_true')
    p.add_argument('--from', dest='from_date', default=None)
    p.add_argument('--to', dest='to_date', default=None)
    p.add_argument('--universe-file', default=None,
                   help='txt file with one ticker per line; defaults to '
                        '/root/openclaw/data/master/universe_top250.txt')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    api_key = os.environ.get('POLYGON_API_KEY')
    if not api_key:
        print("ERROR: POLYGON_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(2)

    # Resolve universe
    if args.universe_file:
        with open(args.universe_file) as f:
            universe = [ln.strip() for ln in f if ln.strip()
                        and not ln.startswith('#')]
    else:
        universe = _default_universe()
    universe = sorted(set(universe))
    print(f"prices_30m ingest — universe={len(universe)} tickers")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = pd.DataFrame() if args.rebuild else load_existing()

    if args.rebuild:
        from_d = args.from_date or '2020-01-01'
    elif not existing.empty:
        last = pd.to_datetime(existing['datetime']).max().date()
        from_d = args.from_date or (last - timedelta(days=1)).isoformat()
    else:
        from_d = args.from_date or (date.today() - timedelta(days=5)).isoformat()
    to_d = args.to_date or date.today().isoformat()
    print(f"  range: {from_d} → {to_d}")

    frames = []
    failures = []
    t0 = time.time()
    for idx, ticker in enumerate(universe, 1):
        try:
            df = _polygon_30m_bars(ticker, api_key, from_d, to_d)
            if not df.empty:
                frames.append(df)
        except Exception as exc:                     # pragma: no cover
            failures.append((ticker, str(exc)))
        if idx % 50 == 0:
            print(f"  ..{idx}/{len(universe)}  elapsed={time.time()-t0:.1f}s")
        time.sleep(0.25)                              # Polygon ≈ 4 rps

    if not frames:
        print("  no new data pulled")
        return

    new_df = pd.concat(frames, ignore_index=True)
    print(f"  new rows        : {len(new_df):,}")

    if not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['ticker', 'datetime'],
                                            keep='last')
    else:
        combined = new_df
    combined = combined.sort_values(['ticker', 'datetime']).reset_index(drop=True)

    print(f"  rows total      : {len(combined):,}")
    print(f"  tickers covered : {combined['ticker'].nunique()}")
    print(f"  last datetime   : {combined['datetime'].max()}")
    if failures:
        print(f"  failures        : {len(failures)}  (first 5: {failures[:5]})")

    if args.dry_run:
        print("  --dry-run: not writing")
        return

    combined.to_parquet(OUT_PATH, index=False)
    print(f"  wrote → {OUT_PATH}")


if __name__ == "__main__":
    main()
