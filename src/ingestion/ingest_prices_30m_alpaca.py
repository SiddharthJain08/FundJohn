"""Alpaca 30-min bars ingester — REPLACES the dead Polygon ingesters
(`ingest_prices_30m.py` / `fetch_30m_bars.py`); POLYGON_API_KEY was purged in the SP-1
cutover, which is why prices_30m.parquet froze at 2026-04-29 with only 5 tickers.

APPEND-ONLY into data/master/prices_30m.parquet (NEVER-DELETE master data, CLAUDE.md
core invariant): load existing → concat → dedup on (ticker, datetime) → write. It NEVER
drops a ticker or truncates the date axis; a runtime guard refuses any write that would.
RTH-filtered (09:30–16:00 ET), schema-matched to the existing parquet
(vw→vwap, n→transactions, tz-aware-UTC `datetime`, ET-calendar `date` string).

Usage:
  PYTHONPATH=src python3 -m ingestion.ingest_prices_30m_alpaca --tickers AAA,BBB --from 2026-04-01 --to 2026-06-01
  or: --universe-file /tmp/b1_universe.txt
"""
from __future__ import annotations
import os
import sys
import time
import argparse
import logging
import pandas as pd
import requests  # noqa: F401 — kept for callers that imported it from here

try:  # src/ on sys.path (ingestion.*) …
    from pipeline.alpaca_multibars import MultiBarsError, fetch_multi_bars, partition_symbols
except ImportError:  # … or ROOT on sys.path
    from src.pipeline.alpaca_multibars import MultiBarsError, fetch_multi_bars, partition_symbols

logger = logging.getLogger(__name__)

DATA_PARQUET = '/root/openclaw/data/master/prices_30m.parquet'
COLUMNS = ['date', 'datetime', 'ticker', 'open', 'high', 'low', 'close', 'volume', 'vwap', 'transactions']
_RENAME = {'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume', 'vw': 'vwap', 'n': 'transactions'}


def _env(key):
    """Read a key from the process env or, failing that, /root/openclaw/.env (the
    worktree has no .env — it is gitignored)."""
    val = os.environ.get(key)
    if val:
        return val
    for line in open('/root/openclaw/.env'):
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None


def _bars_to_df(bars, ticker):
    """Pure: Alpaca bar dicts → schema-matched, RTH-filtered (09:30–16:00 ET) DataFrame.
    `t` is the bar START (UTC). Stored `datetime` stays tz-aware UTC (us); `date` is the
    ET-calendar date string the B1 driver keys `by_date` on."""
    if not bars:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(bars).rename(columns=_RENAME)
    df['datetime'] = pd.to_datetime(df['t'], utc=True).dt.as_unit('us')
    et = df['datetime'].dt.tz_convert('America/New_York')
    df = df[(et.dt.time >= pd.Timestamp('09:30').time()) &
            (et.dt.time < pd.Timestamp('16:00').time())].copy()
    df['ticker'] = ticker
    df['date'] = df['datetime'].dt.tz_convert('America/New_York').dt.strftime('%Y-%m-%d')
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    df['transactions'] = df['transactions'].fillna(0).astype('int64')
    return df[COLUMNS].reset_index(drop=True)


def _merge_append(existing, new):
    """Pure APPEND-ONLY merge: union existing+new, dedup on (ticker, datetime) keeping the
    LAST (fresher) occurrence, sort. Existing tickers/dates are never dropped."""
    if existing is None or len(existing) == 0:
        combined = new
    elif new is None or len(new) == 0:
        combined = existing
    else:
        combined = pd.concat([existing, new], ignore_index=True)
    combined = combined.drop_duplicates(subset=['ticker', 'datetime'], keep='last')
    return combined.sort_values(['ticker', 'datetime']).reset_index(drop=True)


def fetch_many(tickers, start, end):
    """Network: 30-minute bars for MANY tickers through `alpaca data multi-bars`
    (2026-08-22 — was one direct-HTTP request per ticker, no retry). One request
    covers ~200 symbols × 3 days; a --rebuild year chunks at ~2 symbols per
    request (13 bars/day). Returns one _bars_to_df-shaped frame for all tickers,
    keyed by the universe ticker (BRK-B), RTH-filtered. Malformed symbols
    (index/futures/forex forms) are skipped with a warning. Raises
    MultiBarsError on a chunk failure — the caller's run fails loudly, exactly
    as raise_for_status() did."""
    batchable, malformed = partition_symbols(tickers)
    for t in malformed:
        logger.warning('prices_30m: skipping non-stock symbol %s', t)
    if not batchable:
        return pd.DataFrame(columns=COLUMNS)
    got = fetch_multi_bars(list(batchable.keys()), start, end,
                           timeframe='30Min', adjustment='raw', bars_per_day=13)
    frames = [_bars_to_df(got.get(api, []), ticker) for api, ticker in batchable.items()]
    frames = [f for f in frames if len(f)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


def fetch_ticker(ticker, start, end):
    """Single-ticker convenience wrapper over fetch_many (kept for callers/tests)."""
    return fetch_many([ticker], start, end)


def backfill(tickers, start, end, out_path=DATA_PARQUET):
    """Fetch each ticker, APPEND-ONLY merge into the master parquet. Returns (before, after).
    Refuses to write (raises) if any pre-existing ticker would vanish or rows would shrink —
    a hard NEVER-DELETE guard on top of the pure append-only merge."""
    existing = pd.read_parquet(out_path) if os.path.exists(out_path) else pd.DataFrame(columns=COLUMNS)
    before = len(existing)
    before_tickers = set(existing['ticker'].dropna().unique()) if before else set()
    new = fetch_many(list(tickers), start, end)
    counts = new.groupby('ticker').size() if len(new) else {}
    for t in tickers:
        print(f"  [{t}] {int(counts.get(t, 0)) if len(new) else 0} RTH 30m bars", file=sys.stderr)
    merged = _merge_append(existing, new)
    after_tickers = set(merged['ticker'].dropna().unique())
    missing = before_tickers - after_tickers
    if missing:
        raise RuntimeError(f"NEVER-DELETE violation: write would drop tickers {missing}")
    if len(merged) < before:
        raise RuntimeError(f"NEVER-DELETE violation: write would shrink rows {before}→{len(merged)}")
    # Atomic write: a kill/OOM/disk-full mid-write must never corrupt the master parquet.
    tmp = out_path + '.tmp'
    merged.to_parquet(tmp, index=False)
    os.replace(tmp, out_path)   # atomic same-filesystem rename
    return before, len(merged)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tickers', help='comma-separated symbols')
    ap.add_argument('--universe-file', help='newline-separated symbols')
    ap.add_argument('--from', dest='from_date', required=True)
    ap.add_argument('--to', dest='to_date', required=True)
    a = ap.parse_args()
    if a.universe_file:
        tickers = [ln.strip() for ln in open(a.universe_file) if ln.strip() and not ln.startswith('#')]
    else:
        tickers = [t.strip() for t in (a.tickers or '').split(',') if t.strip()]
    if not tickers:
        print('no tickers', file=sys.stderr)
        return 2
    before, after = backfill(tickers, a.from_date, a.to_date)
    print(f"prices_30m rows: {before} -> {after} (+{after - before})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
