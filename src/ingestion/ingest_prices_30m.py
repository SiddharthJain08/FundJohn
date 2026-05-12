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
                      start: str, end: str,
                      max_retries: int = 3) -> pd.DataFrame:
    """One Polygon 30-minute aggregate pull. Returns [] on 404 (no data).

    On 429 the function honours `Retry-After` (or 60s default) and retries
    up to `max_retries` times. The previous behavior of silently returning
    empty on 429 caused fetches to claim "no new data pulled" while
    actually being throttled — see prices_30m incident 2026-04-30."""
    url = (f"{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/30/minute/"
           f"{start}/{end}")
    params = {'adjusted': 'true', 'sort': 'asc', 'limit': 50000, 'apiKey': api_key}
    for attempt in range(max_retries):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 404:
            return pd.DataFrame()
        if r.status_code == 429:
            wait = int(r.headers.get('Retry-After', '60'))
            wait = min(max(wait, 5), 120)
            print(f"  [WARN] 429 for {ticker} — retrying in {wait}s (attempt {attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
        break
    else:
        # Exhausted retries — surface as exception so the caller logs it.
        raise RuntimeError(f"Polygon 429 for {ticker} after {max_retries} retries")
    js = r.json()
    if 'results' not in js or not js['results']:
        return pd.DataFrame()
    df = pd.DataFrame(js['results'])
    # Polygon timestamps are ms since epoch UTC. Keep the datetime column
    # as tz-aware UTC to match the existing parquet schema (datetime64[us,
    # UTC]) — concat-ing tz-naive ET with tz-aware UTC NaTs the new rows.
    df['datetime'] = pd.to_datetime(df['t'], unit='ms', utc=True)
    df = df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low',
                            'c': 'close', 'v': 'volume',
                            'vw': 'vwap', 'n': 'transactions'})
    df['ticker'] = ticker
    # Regular session only: 09:30 – 16:00 ET. Compute the ET projection
    # locally for filtering, but keep the stored datetime as UTC.
    et = df['datetime'].dt.tz_convert('America/New_York')
    df = df[(et.dt.time >= pd.Timestamp('09:30').time()) &
            (et.dt.time <= pd.Timestamp('16:00').time())]
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
            # Surface the first few failures so a systematic problem (auth,
            # tzdata, rate-limit) doesn't masquerade as "no new data pulled".
            if len(failures) <= 3:
                print(f"  [WARN] {ticker}: {str(exc)[:200]}", file=sys.stderr)
        if idx % 50 == 0:
            print(f"  ..{idx}/{len(universe)}  elapsed={time.time()-t0:.1f}s")
        time.sleep(1.0)                               # 1 rps stays under the
                                                       # Massive Options Starter
                                                       # per-minute cap; faster
                                                       # rates trigger silent 429s.

    if not frames:
        print(f"  no new data pulled (failures={len(failures)}/{len(universe)})")
        if failures:
            print(f"  first failure: {failures[0][0]} → {failures[0][1][:200]}", file=sys.stderr)
        return

    new_df = pd.concat(frames, ignore_index=True)
    print(f"  new rows        : {len(new_df):,}")

    if not existing.empty:
        # Both sides must be tz-aware UTC to merge cleanly. Existing parquet
        # already stores datetime64[us, UTC]; new rows are produced tz-aware
        # UTC by _polygon_30m_bars. utc=True in the coerce normalizes both
        # tz-naive and tz-aware inputs to tz-aware UTC.
        existing['datetime'] = pd.to_datetime(existing['datetime'], errors='coerce', utc=True)
        new_df['datetime']   = pd.to_datetime(new_df['datetime'],   errors='coerce', utc=True)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['ticker', 'datetime'],
                                            keep='last')
    else:
        combined = new_df
    combined['datetime'] = pd.to_datetime(combined['datetime'], errors='coerce', utc=True)
    # Drop rows with NaT datetimes — they are write corruption from earlier
    # tz-mismatch merges, not valid market data. This is a corruption fix,
    # not a data-axis truncation: invalid rows have no semantic content.
    nat_dropped = combined['datetime'].isna().sum()
    if nat_dropped:
        combined = combined.dropna(subset=['datetime'])
        print(f"  dropped {nat_dropped} NaT rows (corrupted dtype from prior tz-naive writes)")
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
