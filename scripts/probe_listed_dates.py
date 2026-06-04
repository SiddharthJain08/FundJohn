"""SP-7 Phase A2 — one-shot listed_date probe.

For each alpaca_tradable_universe row with listed_date IS NULL (optionally
scoped --tickers/--universe-file), fetch the EARLIEST Alpaca daily bar and
write its date into listed_date. Resumable by construction (NULL-scoped).

Usage: POSTGRES_URI=... nice -n 19 python3 scripts/probe_listed_dates.py \
           [--tickers A,B] [--universe-file PATH] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg2

ALPACA_BIN = "/root/go/bin/alpaca"


def earliest_bar_date(symbol: str) -> str | None:
    args = [ALPACA_BIN, "data", "bars", "--symbol", symbol,
            "--start", "2000-01-03", "--end", "2026-12-31",
            "--timeframe", "1Day", "--adjustment", "split", "--sort", "asc", "--limit", "1"]
    res = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:200])
    bars = (json.loads(res.stdout) or {}).get("bars") or []
    if not bars:
        return None
    return (bars[0].get("t") or "")[:10] or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=None)
    ap.add_argument("--universe-file", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    scope = None
    if args.tickers:
        scope = [t.strip().upper() for t in args.tickers.split(",")]
    elif args.universe_file:
        scope = [l.strip() for l in Path(args.universe_file).read_text().splitlines() if l.strip()]

    pg = psycopg2.connect(os.environ["POSTGRES_URI"])
    with pg.cursor() as cur:
        if scope:
            cur.execute("SELECT symbol FROM alpaca_tradable_universe "
                        "WHERE listed_date IS NULL AND symbol = ANY(%s) ORDER BY symbol",
                        (scope,))
        else:
            cur.execute("SELECT symbol FROM alpaca_tradable_universe "
                        "WHERE listed_date IS NULL ORDER BY symbol")
        symbols = [r[0] for r in cur.fetchall()]
    if args.limit:
        symbols = symbols[: args.limit]

    done = failed = empty = 0
    for sym in symbols:
        try:
            d = earliest_bar_date(sym)
        except Exception as e:
            sys.stderr.write(f"[probe] {sym}: {e}\n")
            failed += 1
            time.sleep(1.0)
            continue
        if d is None:
            empty += 1
        else:
            with pg.cursor() as cur:
                cur.execute("UPDATE alpaca_tradable_universe SET listed_date=%s "
                            "WHERE symbol=%s", (d, sym))
            pg.commit()
            done += 1
        if (done + failed + empty) % 250 == 0:
            print(f"[probe] {done+failed+empty}/{len(symbols)} done={done}")
        time.sleep(0.05)
    print(f"[probe] DONE total={len(symbols)} set={done} empty={empty} failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
