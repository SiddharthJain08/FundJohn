"""
ticker_metadata_writer.py — Phase A daily writer for ticker_metadata_snapshots.

build_metadata_rows() composes per-ticker rows from:
  - alpaca_rows:   list of Alpaca asset dicts (symbol, asset_class, exchange,
                   status, tradable, shortable, fractionable, easy_to_borrow,
                   first_seen_at, last_seen_at)
  - fmp_profile:   dict[symbol -> FMP /profile response] (sector, industry,
                   mktCap, ipoDate)
  - prices_parquet: dict[symbol -> {"adv_usd_20d": float}]
  - options_cache: dict[symbol -> bool]  (True = options eligible)

write_snapshots() UPSERTs idempotently on (snapshot_date, symbol).

Phase B will add historical membership and market-cap backfill; this file
owns only the live-daily write path.
"""
from __future__ import annotations

from datetime import date

import psycopg2

from src.strategies._sp500_membership import SP500_SET

UPSERT_SQL = """
INSERT INTO ticker_metadata_snapshots (
    snapshot_date, symbol, asset_class, exchange, status,
    tradable, shortable, fractionable, easy_to_borrow,
    market_cap, adv_usd_20d, sector, industry, options_eligible,
    in_sp500, in_r1000, in_r3000, listed_date, delisted_date, source_tag
) VALUES (
    %(snapshot_date)s, %(symbol)s, %(asset_class)s, %(exchange)s, %(status)s,
    %(tradable)s, %(shortable)s, %(fractionable)s, %(easy_to_borrow)s,
    %(market_cap)s, %(adv_usd_20d)s, %(sector)s, %(industry)s, %(options_eligible)s,
    %(in_sp500)s, %(in_r1000)s, %(in_r3000)s, %(listed_date)s, %(delisted_date)s,
    %(source_tag)s
)
ON CONFLICT (snapshot_date, symbol) DO UPDATE SET
    asset_class=EXCLUDED.asset_class,
    exchange=EXCLUDED.exchange,
    status=EXCLUDED.status,
    tradable=EXCLUDED.tradable,
    shortable=EXCLUDED.shortable,
    fractionable=EXCLUDED.fractionable,
    easy_to_borrow=EXCLUDED.easy_to_borrow,
    market_cap=EXCLUDED.market_cap,
    adv_usd_20d=EXCLUDED.adv_usd_20d,
    sector=EXCLUDED.sector,
    industry=EXCLUDED.industry,
    options_eligible=EXCLUDED.options_eligible,
    in_sp500=EXCLUDED.in_sp500,
    in_r1000=EXCLUDED.in_r1000,
    in_r3000=EXCLUDED.in_r3000,
    listed_date=EXCLUDED.listed_date,
    delisted_date=EXCLUDED.delisted_date,
    source_tag=EXCLUDED.source_tag
"""


def _rank_r1000_r3000(rows: list[dict]) -> tuple[set[str], set[str]]:
    """Return (r1000_syms, r3000_syms) ranked by descending market_cap."""
    ranked = sorted(
        ((r["symbol"], r.get("market_cap") or 0.0) for r in rows),
        key=lambda x: -x[1],
    )
    r1000 = {s for s, _ in ranked[:1000]}
    r3000 = {s for s, _ in ranked[:3000]}
    return r1000, r3000


def build_metadata_rows(
    snapshot_date: date,
    alpaca_rows: list[dict],
    fmp_profile: dict[str, dict],
    prices_parquet: dict[str, dict],
    options_cache: dict[str, bool],
    source_tag: str,
) -> list[dict]:
    """
    Compose enriched metadata rows from the four source dicts.

    Returns a list of row-dicts ready for UPSERT.  in_r1000/in_r3000 are
    computed from the market_cap values present in this batch; tickers with
    no market_cap data are ranked last (treated as 0).
    """
    enriched: list[dict] = []
    for a in alpaca_rows:
        sym = a["symbol"]
        p = fmp_profile.get(sym, {})
        pp = prices_parquet.get(sym, {})
        enriched.append({
            "symbol": sym,
            "asset_class": a["asset_class"],
            "exchange": a.get("exchange"),
            "status": a["status"],
            "tradable": a.get("tradable", False),
            "shortable": a.get("shortable", False),
            "fractionable": a.get("fractionable", False),
            "easy_to_borrow": a.get("easy_to_borrow", False),
            "market_cap": p.get("mktCap"),
            "adv_usd_20d": pp.get("adv_usd_20d"),
            "sector": p.get("sector"),
            "industry": p.get("industry"),
            "options_eligible": options_cache.get(sym, False),
            "in_sp500": sym in SP500_SET,
            "in_r1000": False,   # filled below after ranking
            "in_r3000": False,   # filled below after ranking
            "listed_date": p.get("ipoDate") or a.get("first_seen_at"),
            "delisted_date": (
                None if a["status"] == "active" else a.get("last_seen_at")
            ),
        })

    r1000, r3000 = _rank_r1000_r3000(enriched)
    for r in enriched:
        r["in_r1000"] = r["symbol"] in r1000
        r["in_r3000"] = r["symbol"] in r3000
        r["snapshot_date"] = snapshot_date
        r["source_tag"] = source_tag

    return enriched


def write_snapshots(dsn: str, rows: list[dict]) -> int:
    """
    UPSERT rows into ticker_metadata_snapshots.

    Idempotent: re-running with the same (snapshot_date, symbol) key
    overwrites non-key columns with the same values — row count stays 1.

    Returns the number of rows processed (not necessarily inserted; includes
    on-conflict updates).
    """
    written = 0
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(UPSERT_SQL, r)
            written += 1
        conn.commit()
    return written


if __name__ == "__main__":
    import argparse
    import json
    import os
    from datetime import date as _date

    ap = argparse.ArgumentParser(
        description="Ticker metadata writer — bare CLI stub (Phase A)."
    )
    ap.add_argument(
        "--date",
        default=str(_date.today()),
        help="Snapshot date (YYYY-MM-DD). Defaults to today.",
    )
    ap.add_argument(
        "--source-tag",
        default="live_daily",
        help="source_tag value written to every row.",
    )
    args = ap.parse_args()

    # Production data fetching lives in src/pipeline/run_ticker_metadata_step.py.
    # This bare stub is the Task 11 CLI entry point; it validates imports and
    # prints a JSON status so callers can detect success/failure.
    print(json.dumps({"ok": True, "date": args.date, "source_tag": args.source_tag}))
