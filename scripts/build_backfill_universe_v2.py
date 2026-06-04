"""SP-7 A5 — build data/.backfill_universe_v2.txt.

v2 = (tradable ∧ active ∧ easy_to_borrow us_equity) − already-covered.
Artifact is committed (mirrors v1; preflight gate #6 pattern).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2

ROOT = Path("/root/openclaw")
OUT = ROOT / "data" / ".backfill_universe_v2.txt"
PRICES = ROOT / "data" / "master" / "prices.parquet"


def select_v2_universe(rows, covered: set[str]) -> list[str]:
    out = []
    for symbol, tradable, status, etb in rows:
        if not (tradable and status == "active" and etb):
            continue
        norm = symbol.replace(".", "-")
        if norm in covered or symbol in covered:
            continue
        out.append(norm)
    return sorted(set(out))


def main() -> int:
    pg = psycopg2.connect(os.environ["POSTGRES_URI"])
    with pg.cursor() as cur:
        cur.execute("""
            SELECT symbol, tradable, status, easy_to_borrow
              FROM alpaca_tradable_universe
             WHERE asset_class = 'us_equity'
        """)
        rows = cur.fetchall()
    covered = set(pd.read_parquet(PRICES, columns=["ticker"]).ticker.unique())
    universe = select_v2_universe(rows, covered)
    OUT.write_text("\n".join(universe) + "\n")
    print(f"[v2-universe] wrote {len(universe)} tickers to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
