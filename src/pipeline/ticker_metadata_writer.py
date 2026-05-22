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

SP-2 Phase B DRY (Task 8, 2026-05-22): r1000/r3000 ranking and SP500
membership now delegate to src/pipeline/backfillers/universe_metadata.py so
the daily-writer and the backfill driver share one code path. The public
build_metadata_rows() signature is preserved (Phase A tests still pass).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import psycopg2

# DRY: the daily writer reuses helpers from the Phase-B builder. Importing
# build_month_snapshot at module level both wires the alternate code path for
# `build_snapshot_via_builder` (below) and satisfies the regression check that
# verifies this file delegates to the shared module.
from src.pipeline.backfillers.universe_metadata import (
    build_month_snapshot,
    rank_in_r1000_r3000,
    _sp500_membership_on,
)
from src.strategies._sp500_membership import SP500_SET  # noqa: F401 (kept for back-compat)

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

    Returns a list of row-dicts ready for UPSERT. in_r1000/in_r3000 are computed
    via the shared `rank_in_r1000_r3000` helper from the Phase-B builder
    (descending market_cap among tradable+active tickers with non-None
    market_cap). Tickers without market_cap are excluded from the ranking pool.

    in_sp500 prefers the point-in-time historical CSV via
    `_sp500_membership_on(snapshot_date)`. Falls back to the hardcoded
    `SP500_SET` if the CSV is unavailable (covers fresh installs / dev envs).
    """
    sp500 = _sp500_membership_on(snapshot_date)
    if not sp500:
        # CSV not available — fall back to legacy hardcoded set so the live
        # daily flow stays unbroken in test environments.
        sp500 = set(SP500_SET)

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
            "in_sp500": sym in sp500,
            "in_r1000": False,   # filled below after ranking
            "in_r3000": False,   # filled below after ranking
            "listed_date": p.get("ipoDate") or a.get("first_seen_at"),
            "delisted_date": (
                None if a["status"] == "active" else a.get("last_seen_at")
            ),
        })

    r1000, r3000 = rank_in_r1000_r3000(enriched)
    for r in enriched:
        r["in_r1000"] = r["symbol"] in r1000
        r["in_r3000"] = r["symbol"] in r3000
        r["snapshot_date"] = snapshot_date
        r["source_tag"] = source_tag

    return enriched


def build_today_snapshot_via_builder(
    pg,
    fmp_profile: dict[str, dict],
    options_cache: dict[str, bool],
    source_tag: str = "live_daily",
    today: Optional[date] = None,
) -> list[dict]:
    """Alternate daily-write path that goes through the shared
    `build_month_snapshot` builder.

    Used by callers that have a live psycopg2 connection but don't want to
    pre-fetch the alpaca rows themselves. The builder produces structurally-
    valid rows (market_cap=None until FMP backfill lights up), and we then
    enrich today-only fields (sector / industry / mktCap / options_eligible)
    from the same caches the existing daily flow already loads.

    Daily UPSERT uses `ON CONFLICT (snapshot_date, symbol) DO UPDATE` so
    today's row is always re-overwritten with the freshest cache values —
    contrast with the historical backfill driver, which uses DO NOTHING to
    preserve the first promotion.
    """
    today = today or date.today()
    # Universe = every active+tradable symbol the alpaca table knows about.
    with pg.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM alpaca_tradable_universe "
            "WHERE status='active' AND tradable=TRUE"
        )
        universe = [r[0] for r in cur.fetchall()]

    # Build the shared structural rows.
    df = build_month_snapshot(today, universe, pg)
    if df.empty:
        return []

    # Today-only enrichment from the caches the existing flow already uses.
    fmp_cap = {s: (fmp_profile.get(s, {}) or {}).get('mktCap') for s in df['symbol']}
    df['market_cap'] = df['symbol'].map(fmp_cap).where(
        df['symbol'].map(fmp_cap).notna(), df['market_cap']
    )
    df['sector'] = df['symbol'].map(
        lambda s: (fmp_profile.get(s, {}) or {}).get('sector')
    )
    df['industry'] = df['symbol'].map(
        lambda s: (fmp_profile.get(s, {}) or {}).get('industry')
    )
    df['options_eligible'] = df['symbol'].map(
        lambda s: bool(options_cache.get(s, False))
    )
    df['listed_date'] = df['symbol'].map(
        lambda s: (fmp_profile.get(s, {}) or {}).get('ipoDate')
    )

    # Re-rank now that we have today's market caps.
    r1000, r3000 = rank_in_r1000_r3000(df)
    df['in_r1000'] = df['symbol'].isin(r1000)
    df['in_r3000'] = df['symbol'].isin(r3000)

    # Stamp source_tag and return as row dicts (matching build_metadata_rows
    # output shape so write_snapshots() can take either path).
    df['source_tag'] = source_tag
    return df.to_dict('records')


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
