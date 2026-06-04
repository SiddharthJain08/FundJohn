"""SP-7 A5 — activate the v2 universe in universe_config AFTER the price
backfill completes (daily price maintenance envelope until Phase C).

has_options / has_fundamentals stay FALSE: aux layers are scoped to adopted
strategy universes per spec §2 decision 4. Idempotent upsert; never
deactivates anything.

Category overlap check (2026-06-04): zero overlap between .backfill_universe_v2.txt
and existing non-equity active universe_config rows (checked against live DB).
The COALESCE/NULLIF guard on category is retained as cheap insurance against
future drift: screener_candidate rows are recategorized to 'equity'; real
categories (etf/index/commodity/crypto/forex) are preserved for any future
overlap. has_options/has_fundamentals are NOT touched on conflict — existing
rows (e.g., DELL with has_options=true from SP500 activation) keep their flags.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import psycopg2

V2 = Path("/root/openclaw/data/.backfill_universe_v2.txt")
NOTE = f"sp7-v2-activation {date.today().isoformat()}"
MEMBERSHIP = "SP500_PLUS"  # label only; index_membership is a text[] tag


def build_upsert_params(tickers: list[str], note: str) -> list[tuple]:
    return [(t, "{%s}" % MEMBERSHIP, True, "equity", False, False, 3650, note)
            for t in tickers]


def main() -> int:
    tickers = [l.strip() for l in V2.read_text().splitlines() if l.strip()]
    pg = psycopg2.connect(os.environ["POSTGRES_URI"])
    with pg.cursor() as cur:
        cur.executemany("""
            INSERT INTO universe_config
                (ticker, index_membership, active, category,
                 has_options, has_fundamentals, min_history_days, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker) DO UPDATE SET
                active = true,
                category = COALESCE(
                    NULLIF(universe_config.category, 'screener_candidate'),
                    'equity'
                ),
                notes = COALESCE(universe_config.notes || ' | ', '') || EXCLUDED.notes
        """, build_upsert_params(tickers, NOTE))
    pg.commit()
    print(f"[activate-v2] upserted {len(tickers)} tickers (aux flags FALSE)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
