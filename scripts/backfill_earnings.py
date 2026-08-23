#!/usr/bin/env python3
"""One-shot DEEP backfill of data/master/earnings.parquet from FMP.

Ported 2026-08-23 from /api/v3/historical/earning_calendar/{sym} (403
"Legacy Endpoint" for this key) to /stable/earnings?symbol= — verified
payload: [{symbol, date, epsActual, epsEstimated, revenueActual,
revenueEstimated, lastUpdated}], newest first, upcoming quarters included
with epsActual null.

This is NOT the daily feeder. src/ingestion/ingest_earnings_master.py
(yfinance) keeps the master current; run THIS only to deepen history
(up to QUARTERS per ticker) after a long gap or for a new universe slice.
Rows land in the master's LIVE schema (ticker, date, eps_actual,
eps_estimated, revenue_actual, revenue_estimated, last_updated); existing
(ticker, date) rows are never mutated — new rows are appended only
(CLAUDE.md append-only rule) and the write is atomic (tmp + os.replace).

Usage:
    python3 scripts/backfill_earnings.py                      # prices.parquet universe
    python3 scripts/backfill_earnings.py --tickers AAPL,MSFT
    python3 scripts/backfill_earnings.py --limit 50 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from pathlib import Path

import aiohttp
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

ROOT        = Path(__file__).resolve().parent.parent
MASTER      = ROOT / "data" / "master"
FMP_EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings"
FMP_KEY     = os.environ.get("FMP_API_KEY", "")
CONCURRENCY = 5      # FMP Starter: 300 req/min
QUARTERS    = 40     # ~10 years of quarterly data

MASTER_COLUMNS = ['ticker', 'date', 'eps_actual', 'eps_estimated',
                  'revenue_actual', 'revenue_estimated', 'last_updated']


def rows_from_stable(ticker: str, data: list, today: date) -> list[dict]:
    """Map /stable/earnings rows onto the master schema. Rows without a date
    are dropped; upcoming quarters keep eps_actual None."""
    out = []
    for row in data or []:
        if not isinstance(row, dict) or not row.get("date"):
            continue
        out.append({
            "ticker":            ticker,
            "date":              str(row["date"])[:10],
            "eps_actual":        row.get("epsActual"),
            "eps_estimated":     row.get("epsEstimated"),
            "revenue_actual":    row.get("revenueActual"),
            "revenue_estimated": row.get("revenueEstimated"),
            "last_updated":      today.isoformat(),
        })
    return out


async def fetch_earnings(session, sem, ticker):
    params = {"symbol": ticker, "limit": QUARTERS, "apikey": FMP_KEY}
    async with sem:
        try:
            async with session.get(FMP_EARNINGS_URL, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    return ticker, (data if isinstance(data, list) else [])
                if r.status in (401, 402, 403):
                    raise RuntimeError(f"FMP HTTP {r.status} on /stable/earnings — key/plan problem")
                log.warning(f"{ticker}: HTTP {r.status}")
        except RuntimeError:
            raise
        except Exception as e:
            log.warning(f"{ticker}: {e}")
    return ticker, []


def merge_append_only(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Append rows whose (ticker, date) key is not already in the master.
    Existing rows are kept verbatim and in order — INCLUDING the master's
    pre-existing duplicate keys (101 on 2026-08-23): the CLAUDE.md invariant
    is append-only, and a `drop_duplicates(keep='first')` over the union
    would silently delete them."""
    def _key(df):
        return list(zip(df['ticker'].astype(str),
                        pd.to_datetime(df['date'], errors='coerce').dt.strftime('%Y-%m-%d')))
    if existing is None or existing.empty:
        return new.drop_duplicates(subset=['ticker', 'date']).reset_index(drop=True)
    if new is None or new.empty:
        return existing.reset_index(drop=True)
    seen = set(_key(existing))
    nw = new.copy()
    nw['_k'] = _key(nw)
    nw = nw[~nw['_k'].isin(seen)].drop(columns='_k').drop_duplicates(subset=['ticker', 'date'])
    return pd.concat([existing, nw], ignore_index=True)


def _universe(args) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    prices_path = MASTER / "prices.parquet"
    tickers = sorted(pd.read_parquet(prices_path, columns=["ticker"])["ticker"].unique().tolist())
    return tickers


def _atomic_to_parquet(df: pd.DataFrame, out_path: Path) -> None:
    tmp = out_path.with_name(f".{out_path.name}.tmp.{os.getpid()}")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, out_path)


async def main(argv=None):
    ap = argparse.ArgumentParser(prog="backfill_earnings")
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--limit", type=int, default=None, help="first N tickers of the universe")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not FMP_KEY:
        raise RuntimeError("FMP_API_KEY not set in environment")

    tickers = _universe(args)
    if args.limit is not None:
        tickers = tickers[:args.limit]
    log.info(f"Universe: {len(tickers)} tickers")

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=20)
    today = date.today()

    records = []
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_earnings(session, sem, t) for t in tickers]
        total = len(tasks)
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            ticker, data = await coro
            records.extend(rows_from_stable(ticker, data, today))
            if i % 50 == 0 or i == total:
                log.info(f"  Progress: {i}/{total} tickers | {len(records)} rows so far")

    if not records:
        log.error("No records collected — check FMP_API_KEY")
        return

    df = pd.DataFrame(records, columns=MASTER_COLUMNS)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "ticker"])

    out_path = MASTER / "earnings.parquet"
    before = 0
    if out_path.exists():
        existing = pd.read_parquet(out_path)
        before = len(existing)
        existing["date"] = pd.to_datetime(existing["date"], errors="coerce")
        # Append-only: existing rows (duplicates included) are never dropped.
        df = merge_append_only(existing, df)
    else:
        df = merge_append_only(None, df)

    log.info(f"rows: existing={before} fetched={len(records)} merged={len(df)} added={len(df) - before}")
    if args.dry_run:
        log.info("dry-run: not writing")
        return
    _atomic_to_parquet(df, out_path)
    log.info(f"Written {len(df)} rows -> {out_path}")
    log.info(f"Date range: {df.date.min().date()} -> {df.date.max().date()}")
    log.info(f"Tickers with data: {df.ticker.nunique()}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
