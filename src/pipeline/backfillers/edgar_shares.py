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
      [--covered-only | --alpaca-active] [--max-tickers N] [--dry-run]

Weekly schedule (2026-08-23, openclaw-edgar-shares.timer, Sat 03:00 UTC):
  --alpaca-active --max-tickers 2000. The store had NO refresh path and froze
  at fetched_at 2026-06-04. A full sweep of the ~7.1k CIK-mapped Alpaca
  equities is ~0.7 s/ticker (4-8 MB companyfacts JSON + parse + append +
  0.12 s sleep) ~= 80 min and ~28 GB/week, so each run takes a BOUNDED slice:
  never-attempted tickers first, then the stalest by last ATTEMPT (fetch log
  data/master/.shares_outstanding_fetch_log.json — the parquet's fetched_at
  only moves when a NEW row lands, so it cannot order attempts). 2000/run
  ~= 25 min; the universe cycles every ~4 weeks, inside the 13-week 10-Q
  cadence. A RuntimeMaxSec kill loses nothing: appends are per ticker and the
  log is flushed every 100 tickers.
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
FETCH_LOG = ROOT / "data" / "master" / ".shares_outstanding_fetch_log.json"
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


# ── Weekly-refresh helpers (fetch log, ordering, Alpaca universe) ───────────

def load_fetch_log(path=FETCH_LOG) -> dict[str, str]:
    """{ticker: ISO ts of last fetch ATTEMPT}. Missing/corrupt -> {}."""
    path = Path(path)
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_fetch_log(path, updates: dict[str, str]) -> None:
    """Merge `updates` into the on-disk log; atomic tmp + os.replace."""
    import os
    path = Path(path)
    merged = {**load_fetch_log(path), **updates}
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(merged, sort_keys=True))
    os.replace(tmp, path)


def order_by_fetch_log(universe: list[str], fetch_log: dict[str, str],
                       max_tickers: int | None = None) -> list[str]:
    """Never-attempted tickers first (alphabetical), then ascending by last
    attempt; optionally truncated to the first max_tickers."""
    ordered = sorted(universe, key=lambda t: (t in fetch_log, fetch_log.get(t, ""), t))
    if max_tickers is not None and max_tickers >= 0:
        ordered = ordered[:max_tickers]
    return ordered


def _connect_pg(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn)


def alpaca_active_universe(dsn: str) -> list[str]:
    """Active, tradable US equities from alpaca_tradable_universe — the same
    symbol set run_ticker_metadata_step / market_cap_lookup are keyed on."""
    conn = _connect_pg(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT symbol FROM alpaca_tradable_universe
                   WHERE status = 'active' AND tradable = TRUE
                     AND asset_class = 'us_equity'
                   ORDER BY symbol"""
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _load_universe(args) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.universe_file:
        return [l.strip() for l in Path(args.universe_file).read_text().splitlines() if l.strip()]
    if args.covered_only:
        df = pd.read_parquet(PRICES_PARQUET, columns=["ticker"])
        return sorted(df.ticker.unique().tolist())
    if getattr(args, "alpaca_active", False):
        import os
        dsn = os.environ.get("POSTGRES_URI", "")
        if not dsn:
            raise SystemExit("--alpaca-active needs POSTGRES_URI")
        return alpaca_active_universe(dsn)
    raise SystemExit("one of --tickers / --universe-file / --covered-only / "
                     "--alpaca-active required")


def main() -> int:
    ap = argparse.ArgumentParser(prog="edgar_shares")
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--universe-file", default=None)
    ap.add_argument("--covered-only", action="store_true")
    ap.add_argument("--alpaca-active", action="store_true",
                    help="universe = active tradable us_equity rows of alpaca_tradable_universe")
    ap.add_argument("--max-tickers", type=int, default=None,
                    help="bounded slice per run: never-attempted first, then stalest by fetch log")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cik_map: dict[str, str] = json.loads(CIK_MAP.read_text())
    universe = _load_universe(args)
    # Only CIK-mapped names count toward the slice; the rest are free skips.
    no_cik = sum(1 for t in universe
                 if not (cik_map.get(t) or cik_map.get(t.replace("-", "."))))
    universe = [t for t in universe
                if cik_map.get(t) or cik_map.get(t.replace("-", "."))]
    fetch_log = load_fetch_log(FETCH_LOG)
    universe = order_by_fetch_log(universe, fetch_log, args.max_tickers)
    never_attempted = sum(1 for t in universe if t not in fetch_log)
    print(f"[edgar] universe={len(universe)} (no_cik skipped={no_cik}, "
          f"never_attempted={never_attempted}, max_tickers={args.max_tickers})")

    total_added, no_facts, failed = 0, 0, 0
    pending: dict[str, str] = {}
    t0 = time.time()
    for i, ticker in enumerate(universe):
        # CIK map keys use dot share-class form sometimes; try both.
        cik = cik_map.get(ticker) or cik_map.get(ticker.replace("-", "."))
        try:
            facts = fetch_companyfacts(cik)
        except Exception as e:
            failed += 1
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
        if not args.dry_run:
            pending[ticker] = datetime.now(timezone.utc).isoformat()
            if len(pending) >= 100:
                save_fetch_log(FETCH_LOG, pending)
                pending = {}
        if (i + 1) % 250 == 0:
            print(f"[edgar] {i+1}/{len(universe)} done, +{total_added} rows, "
                  f"{time.time() - t0:.0f}s elapsed")
        time.sleep(0.12)  # ~8 req/s, under SEC's 10/s ceiling
    if pending:
        save_fetch_log(FETCH_LOG, pending)
    print(f"[edgar] DONE universe={len(universe)} added={total_added} "
          f"no_cik={no_cik} no_facts={no_facts} failed={failed} "
          f"elapsed={time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
