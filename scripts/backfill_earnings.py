#!/usr/bin/env python3
"""
Backfill earnings calendar from FMP historical endpoint.
Fetches ~10 years (40 quarters) for all tickers in prices.parquet.
Usage: python3 scripts/backfill_earnings.py
"""
import asyncio, os, logging
from pathlib import Path
import aiohttp
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

ROOT        = Path(__file__).resolve().parent.parent
MASTER      = ROOT / "data" / "master"
FMP_BASE    = "https://financialmodelingprep.com/api/v3"
FMP_KEY     = os.environ.get("FMP_API_KEY", "")
CONCURRENCY = 5      # FMP Starter: 300 req/min
QUARTERS    = 40     # ~10 years of quarterly data

async def fetch_earnings(session, sem, ticker):
    url = f"{FMP_BASE}/historical/earning_calendar/{ticker}"
    params = {"limit": QUARTERS, "apikey": FMP_KEY}
    async with sem:
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json()
                    if data:
                        return ticker, data
                else:
                    log.warning(f"{ticker}: HTTP {r.status}")
        except Exception as e:
            log.warning(f"{ticker}: {e}")
    return ticker, []

async def main():
    if not FMP_KEY:
        raise RuntimeError("FMP_API_KEY not set in environment")

    # Load universe from prices.parquet
    prices_path = MASTER / "prices.parquet"
    tickers = sorted(pd.read_parquet(prices_path, columns=["ticker"])["ticker"].unique().tolist())
    log.info(f"Universe: {len(tickers)} tickers")

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=20)

    records = []
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_earnings(session, sem, t) for t in tickers]
        total = len(tasks)
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            ticker, data = await coro
            for row in data:
                records.append({
                    "ticker":               ticker,
                    "date":                 row.get("date"),
                    "eps_actual":           row.get("eps"),
                    "eps_estimated":        row.get("epsEstimated"),
                    "revenue_actual":       row.get("revenue"),
                    "revenue_estimated":    row.get("revenueEstimated"),
                    "fiscal_date_ending":   row.get("fiscalDateEnding"),
                    "updated_from_date":    row.get("updatedFromDate"),
                })
            if i % 50 == 0 or i == total:
                log.info(f"  Progress: {i}/{total} tickers | {len(records)} rows so far")

    if not records:
        log.error("No records collected — check FMP_API_KEY")
        return

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "ticker"])
    df = df.sort_values(["ticker", "date"]).drop_duplicates(subset=["ticker", "date"])

    out_path = MASTER / "earnings.parquet"

    # Merge with existing if present (incremental safe)
    if out_path.exists():
        existing = pd.read_parquet(out_path)
        existing["date"] = pd.to_datetime(existing["date"], errors="coerce")
        df = pd.concat([existing, df], ignore_index=True)
        df = df.sort_values(["ticker", "date"]).drop_duplicates(subset=["ticker", "date"])

    df.to_parquet(out_path, index=False)
    log.info(f"Written {len(df)} rows -> {out_path}")
    log.info(f"Date range: {df.date.min().date()} -> {df.date.max().date()}")
    log.info(f"Tickers with data: {df.ticker.nunique()}")

if __name__ == "__main__":
    asyncio.run(main())
