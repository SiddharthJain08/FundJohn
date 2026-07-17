"""SP-7 Phase A1 — EDGAR shares-outstanding ingester.

Fetches SEC companyfacts per CIK and appends shares-outstanding rows to
data/master/shares_outstanding.parquet (append-only — member of the
NEVER-DELETE family; existing (ticker, asof_date) rows are never mutated).

Source tags: dei.EntityCommonStockSharesOutstanding (primary, cover-page
entity total) + us-gaap.CommonStockSharesOutstanding (fallback/older filings).
Multi-class entities report one entity-level total — adequate for cap-tier
ranking (documented caveat in the SP-7 spec §3 A1).

SEC fair-access: <=10 req/s, descriptive User-Agent REQUIRED (default
python-urllib UA gets Cloudflare-1010-style blocks — same lesson as the
Discord webhook 403s, memory: reference_discord_urllib_cloudflare_ua).

Usage:
  POSTGRES_URI=... python3 -m src.pipeline.backfillers.edgar_shares \
      [--tickers NVDA,AAPL] [--universe-file data/.backfill_universe_v2.txt] \
      [--covered-only] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.data.parquet_store import append_insert_only, row_count

ROOT = Path("/root/openclaw")
SHARES_PARQUET = ROOT / "data" / "master" / "shares_outstanding.parquet"
CIK_MAP = ROOT / "data" / "master" / "_sec_ticker_cik.json"
PRICES_PARQUET = ROOT / "data" / "master" / "prices.parquet"
UA = "OpenClaw research (contact@fundjohn.ai)"
MIN_SHARES, MAX_SHARES = 1e6, 2e11  # unit-sanity gates (spec §7)


def fetch_companyfacts(cik_padded: str) -> dict:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def parse_shares_series(ticker: str, facts: dict) -> list[dict]:
    """Extract deduped shares series. Latest `filed` wins per asof_date.

    Returns [] when the entity reports no shares facts (funds, some ADRs).

    Dedup asymmetry: within a run, latest-`filed` wins (amendments supersede);
    across runs, append_insert_only keeps the first-persisted value (acceptable
    for cap-tier ranking — corrections are unlikely to change tier assignment).
    """
    entries: list[dict] = []
    for taxonomy, tag in (
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    ):
        units = (
            facts.get("facts", {}).get(taxonomy, {}).get(tag, {}).get("units", {})
        )
        for item in units.get("shares", []):
            end, val = item.get("end"), item.get("val")
            if not end or val is None:
                continue
            if not (MIN_SHARES <= float(val) <= MAX_SHARES):
                continue
            entries.append({
                "ticker": ticker,
                "asof_date": end,
                "shares": float(val),
                "form": item.get("form") or "",
                "filed": item.get("filed") or "",
            })
    # Dedupe per asof_date: prefer the latest `filed` (amendments supersede).
    best: dict[str, dict] = {}
    for e in entries:
        cur = best.get(e["asof_date"])
        if cur is None or e["filed"] > cur["filed"]:
            best[e["asof_date"]] = e
    return sorted(best.values(), key=lambda r: r["asof_date"])


def merge_append_only(parquet_path, new_rows: list[dict]) -> int:
    """Append rows whose (ticker, asof_date) is not already present.

    Existing rows are NEVER mutated or dropped (NEVER-DELETE invariant).
    Delegates to append_insert_only for fcntl flock serialization, snappy/
    pyarrow compression, and correct empty-input handling. Returns number
    of rows added (not total row count).
    """
    if not new_rows:
        return 0
    parquet_path = Path(parquet_path)
    fetched_at = datetime.now(timezone.utc).isoformat()
    new_df = pd.DataFrame([{**r, "fetched_at": fetched_at} for r in new_rows])
    before = row_count(parquet_path)
    after = append_insert_only(parquet_path, new_df, key_cols=["ticker", "asof_date"])
    return after - before


def _load_universe(args) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.universe_file:
        return [l.strip() for l in Path(args.universe_file).read_text().splitlines() if l.strip()]
    if args.covered_only:
        df = pd.read_parquet(PRICES_PARQUET, columns=["ticker"])
        return sorted(df.ticker.unique().tolist())
    raise SystemExit("one of --tickers / --universe-file / --covered-only required")


def main() -> int:
    ap = argparse.ArgumentParser(prog="edgar_shares")
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--universe-file", default=None)
    ap.add_argument("--covered-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cik_map: dict[str, str] = json.loads(CIK_MAP.read_text())
    universe = _load_universe(args)
    total_added, no_cik, no_facts = 0, 0, 0
    for i, ticker in enumerate(universe):
        # CIK map keys use dot share-class form sometimes; try both.
        cik = cik_map.get(ticker) or cik_map.get(ticker.replace("-", "."))
        if not cik:
            no_cik += 1
            continue
        try:
            facts = fetch_companyfacts(cik)
        except Exception as e:
            sys.stderr.write(f"[edgar] {ticker}: fetch failed: {e}\n")
            time.sleep(1.0)
            continue
        rows = parse_shares_series(ticker, facts)
        if not rows:
            no_facts += 1
        elif args.dry_run:
            print(f"[dry-run] {ticker}: {len(rows)} share rows")
        else:
            total_added += merge_append_only(SHARES_PARQUET, rows)
        if (i + 1) % 250 == 0:
            print(f"[edgar] {i+1}/{len(universe)} done, +{total_added} rows")
        time.sleep(0.12)  # ~8 req/s, under SEC's 10/s ceiling
    print(f"[edgar] DONE universe={len(universe)} added={total_added} "
          f"no_cik={no_cik} no_facts={no_facts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
