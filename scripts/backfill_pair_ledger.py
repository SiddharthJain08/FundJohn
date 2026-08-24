#!/usr/bin/env python3
"""Task D1/X1 — historical backfill driver for data/derived/pair_ledger.parquet.

Runs src.pipeline.pairs_scanner.run_scan() once per Monday in [--start, --end],
sequentially, IN-PROCESS (imports the scanner's run_scan function directly --
never spawns one python subprocess per week), logging the per-scan summary
line the scanner itself would print.

Documented proxy (survivorship caveat): this backfill uses CURRENT universe
membership (today's active rows in the `universe` table, or `universe_config`
when `universe` is empty -- see pairs_scanner._fetch_active_universe) for
every historical as_of date, not the universe as it stood on that historical
date. That means a ticker that has since been delisted/deactivated is
invisible even for weeks when it may have been tradeable, and — more
importantly for pairs statistics — a currently-active ticker that hadn't
listed yet as of some historical as_of is still included; the per-pair
coverage filter in the scanner (`min_obs_frac` of the trailing window must be
non-NaN closes) excludes those recent listings naturally, since they won't
have enough historical price history to fill the window. This is the same
proxy used elsewhere in this codebase's backfill scripts pending a proper
point-in-time universe table.

A SEPARATE, RANKING-level survivorship proxy also applies within each
historical scan: `pairs_scanner.build_buckets`'s bucket-cap-at-50 ordering
sorts candidates by TODAY's `market_cap` (or the discovered liquidity column)
for every as_of in this backfill, not the market_cap as it stood on that
historical date. A ticker whose market cap has since grown/shrunk
substantially is therefore ranked into or out of a historical week's
top-50-per-bucket using information that wasn't actually available on that
date -- it's a look-ahead in RANKING order, distinct from the membership
survivorship caveat above (which is about which tickers are visible at all).
Both proxies are accepted here pending a proper point-in-time universe/
market-cap table; neither affects the per-pair math (coint/half-life/cost)
once a pair is selected into a bucket.

Usage:
  python3 scripts/backfill_pair_ledger.py --start 2023-09-04 --end 2026-08-24 \
      [--window 504] [--min-corr 0.6] [--fdr-q 0.10] [--cost-k 2.0] \
      [--out data/derived/pair_ledger.parquet]
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Inject ROOT/src so `from pipeline.pairs_scanner import run_scan` resolves
# when this script is invoked directly (python3 scripts/backfill_pair_ledger.py),
# matching the convention in scripts/backfill_universe_5y.py.
sys.path.insert(0, str(ROOT / "src"))

from pipeline.pairs_scanner import (  # noqa: E402
    DEFAULT_COST_K,
    DEFAULT_FDR_Q,
    DEFAULT_LEDGER_PATH,
    DEFAULT_MIN_CORR,
    DEFAULT_UNIVERSE_TABLE,
    DEFAULT_WINDOW,
    PairsScannerDataError,
    run_scan,
)


def iter_mondays(start: dt.date, end: dt.date):
    """Yield every Monday in [start, end], inclusive."""
    d = start + dt.timedelta(days=(7 - start.weekday()) % 7) if start.weekday() != 0 else start
    while d <= end:
        yield d
        d += dt.timedelta(days=7)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (first week's Monday >= this date)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (last week's Monday <= this date)")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--min-corr", type=float, default=DEFAULT_MIN_CORR)
    ap.add_argument("--fdr-q", type=float, default=DEFAULT_FDR_Q)
    ap.add_argument("--cost-k", type=float, default=DEFAULT_COST_K)
    ap.add_argument("--out", default=DEFAULT_LEDGER_PATH)
    ap.add_argument("--universe-table", default=DEFAULT_UNIVERSE_TABLE,
                     help="Postgres table to read the active universe from "
                          "(default: universe, per spec; threaded through to "
                          "pairs_scanner.run_scan -- see that module's "
                          "--universe-table for the universe/universe_config "
                          "fallback behavior).")
    args = ap.parse_args(argv)

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if end < start:
        print(f"[backfill-pair-ledger] ERROR: --end {end} is before --start {start}", file=sys.stderr)
        return 1

    mondays = list(iter_mondays(start, end))
    print(f"[backfill-pair-ledger] {len(mondays)} weekly scans queued "
          f"({start.isoformat()} .. {end.isoformat()}), sequential in-process")

    failures = 0
    for monday in mondays:
        try:
            summary = run_scan(
                as_of=monday, window=args.window, min_corr=args.min_corr,
                fdr_q_threshold=args.fdr_q, cost_k=args.cost_k, out_path=args.out,
                universe_table=args.universe_table,
            )
        except PairsScannerDataError as exc:
            failures += 1
            print(f"[backfill-pair-ledger] as_of={monday.isoformat()} ERROR: {exc}", file=sys.stderr)
            continue
        print(
            f"[pairs-scanner] as_of={monday.isoformat()} buckets={summary['buckets']} "
            f"pairs_tested={summary['pairs_tested']} fdr_pass={summary['fdr_pass']} "
            f"approved={summary['approved']} errors_dropped={summary['errors_dropped']}"
        )

    print(f"[backfill-pair-ledger] done: {len(mondays) - failures}/{len(mondays)} scans succeeded")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
