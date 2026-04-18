#!/usr/bin/env python3
"""
fetch_30m_bars.py

Fetches 30-minute OHLCV + VWAP bars for SPY from the Polygon REST API
(v2/aggs endpoint) and persists to data/master/prices_30m.parquet.

Free tier: 5 req/min, ~2yr history. Starter tier: no rate limit, full history.
Run daily after market close to keep prices_30m.parquet up to date.

Schema (prices_30m.parquet):
  date      — YYYY-MM-DD string
  datetime  — ISO-8601 UTC string
  ticker    — e.g. "SPY"
  open, high, low, close — float
  volume    — int
  vwap      — float (VWAP over the 30-min bar, from Polygon)
  transactions — int (optional, 0 if absent)
"""

import os
import sys
import json
import time
import logging
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

POLYGON_API_KEY = os.environ.get('POLYGON_API_KEY', '')
PARQUET_PATH    = Path(__file__).resolve().parents[2] / 'data' / 'master' / 'prices_30m.parquet'
TICKERS         = ['SPY']  # Phase 3: SPY only. Extend for Phase 4.
BACKFILL_DAYS   = 365 * 2  # 2 years (Polygon free tier max)
MAX_RETRIES     = 3
RATE_LIMIT_WAIT = 12        # seconds between requests on free tier (5 req/min)

logger = logging.getLogger(__name__)


def _polygon_aggs(ticker: str, start: str, end: str) -> list[dict]:
    """
    Fetch 30-min bars via Polygon v2/aggs.
    Returns list of bar dicts: {t, o, h, l, c, v, vw, n}
    """
    url = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}"
           f"/range/30/minute/{start}/{end}"
           f"?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_API_KEY}")
    bars: list[dict] = []
    retries = 0
    while url:
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(url, timeout=30)
                resp.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2 ** attempt)
        data = resp.json()
        if data.get('status') == 'ERROR':
            raise RuntimeError(f"Polygon error: {data.get('error')}")
        bars.extend(data.get('results', []))
        url = data.get('next_url')  # pagination
        if url:
            url = url + f"&apiKey={POLYGON_API_KEY}"
            time.sleep(RATE_LIMIT_WAIT)
    return bars


def _bars_to_df(bars: list[dict], ticker: str) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    rows = []
    for b in bars:
        ts  = b['t'] / 1000  # unix seconds
        dt  = datetime.fromtimestamp(ts, tz=timezone.utc)
        rows.append({
            'date':         dt.strftime('%Y-%m-%d'),
            'datetime':     dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'ticker':       ticker,
            'open':         float(b.get('o', 0)),
            'high':         float(b.get('h', 0)),
            'low':          float(b.get('l', 0)),
            'close':        float(b.get('c', 0)),
            'volume':       int(b.get('v', 0)),
            'vwap':         float(b.get('vw', b.get('c', 0))),
            'transactions': int(b.get('n', 0)),
        })
    return pd.DataFrame(rows)


def _get_existing_max_date(ticker: str) -> Optional[str]:
    if not PARQUET_PATH.exists():
        return None
    try:
        df = pd.read_parquet(PARQUET_PATH, columns=['ticker', 'date'])
        sub = df[df['ticker'] == ticker]
        return str(sub['date'].max()) if not sub.empty else None
    except Exception:
        return None


def _append_to_parquet(new_df: pd.DataFrame) -> int:
    if new_df.empty:
        return 0
    if PARQUET_PATH.exists():
        try:
            existing = pd.read_parquet(PARQUET_PATH)
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=['datetime', 'ticker'])
            combined = combined.sort_values(['ticker', 'datetime'])
            combined.to_parquet(PARQUET_PATH, index=False)
            return len(new_df)
        except Exception as e:
            logger.warning(f"Append failed, overwriting: {e}")
    PARQUET_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_df.to_parquet(PARQUET_PATH, index=False)
    return len(new_df)


def fetch_ticker(ticker: str) -> dict:
    max_date = _get_existing_max_date(ticker)
    if max_date:
        start = (date.fromisoformat(max_date) + timedelta(days=1)).isoformat()
    else:
        start = (date.today() - timedelta(days=BACKFILL_DAYS)).isoformat()

    end = date.today().isoformat()

    if start >= end:
        return {'ticker': ticker, 'inserted': 0, 'status': 'up_to_date'}

    print(f'  {ticker}: fetching {start} → {end}', file=sys.stderr)
    try:
        bars = _polygon_aggs(ticker, start, end)
        df   = _bars_to_df(bars, ticker)
        n    = _append_to_parquet(df)
        print(f'  {ticker}: +{n} bars', file=sys.stderr)
        return {'ticker': ticker, 'inserted': n, 'status': 'ok'}
    except Exception as e:
        print(f'  {ticker}: ERROR — {e}', file=sys.stderr)
        return {'ticker': ticker, 'inserted': 0, 'status': 'error', 'error': str(e)}


def main() -> dict:
    results = {}
    for i, ticker in enumerate(TICKERS):
        if i > 0:
            time.sleep(RATE_LIMIT_WAIT)
        results[ticker] = fetch_ticker(ticker)
    return results


if __name__ == '__main__':
    out = main()
    print(json.dumps({'status': 'ok', 'results': out}))
