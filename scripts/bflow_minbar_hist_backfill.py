#!/usr/bin/env python3
"""SP-6 B-flow — HISTORICAL minute-bar backfill (Phase-1b historical kill-test).

Prereg: docs/superpowers/specs/2026-06-06-sp6-bflow-phase1b-historical-killtest-prereg.md

Pulls Alpaca SIP 1-minute RTH bars for the FROZEN 505-ticker universe over the
pre-registered window [2023-01-03, 2026-03-31] into the DEDICATED historical
cache ``data/cache/min_bars_hist/``. The live accrual cache
(``data/cache/min_bars/``) is never touched.

- Session calendar = SPY dates in data/master/prices.parquet (read-only).
- Fetch goes through the frozen ``minbar_cache.get_session_bars`` (cache-hit
  skip + atomic write + invalid-symbol ejection + 429-retry), so the pull is
  RESUMABLE for free: rerunning skips completed sessions.
- NO-PEEK DISCIPLINE (prereg §6): logs ticker/row/zero-bar COUNTS only. No
  IC / return / feature statistic is computed here.

Usage:
    python3 scripts/bflow_minbar_hist_backfill.py [--limit N]
        [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

HIST_CACHE_DIR = os.path.join(_ROOT, "data", "cache", "min_bars_hist")
UNIVERSE_PATH = os.path.join(_ROOT, "analysis", "bflow_phase1b_hist",
                             "universe_505.txt")
PRICES_PARQUET = os.path.join(_ROOT, "data", "master", "prices.parquet")

WINDOW_START = "2023-01-03"
WINDOW_END = "2026-03-31"


def load_universe(path=UNIVERSE_PATH):
    with open(path) as fh:
        tickers = [ln.strip() for ln in fh if ln.strip()]
    if len(tickers) != 505:
        raise SystemExit(f"frozen universe expected 505 tickers, got "
                         f"{len(tickers)} — refusing (prereg violation)")
    return tickers


def load_sessions(start, end, prices_path=PRICES_PARQUET):
    """SPY dates in master prices.parquet within [start, end], ascending."""
    import pandas as pd
    df = pd.read_parquet(prices_path, columns=["ticker", "date"])
    dates = sorted(set(df.loc[df["ticker"] == "SPY", "date"]))
    return [d for d in dates if start <= d <= end]


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="bflow_minbar_hist_backfill")
    p.add_argument("--limit", type=int, default=None,
                   help="only the first N sessions (smoke)")
    p.add_argument("--start", default=WINDOW_START)
    p.add_argument("--end", default=WINDOW_END)
    p.add_argument("--dry-run", action="store_true",
                   help="enumerate sessions + universe; fetch NOTHING")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args([] if argv is None else argv)

    if not (WINDOW_START <= args.start <= args.end <= WINDOW_END):
        raise SystemExit(f"--start/--end must stay inside the pre-registered "
                         f"window [{WINDOW_START}, {WINDOW_END}]")

    tickers = load_universe()
    sessions = load_sessions(args.start, args.end)
    if args.limit is not None:
        sessions = sessions[:args.limit]

    print(f"[bflow-hist] universe={len(tickers)} tickers, "
          f"{len(sessions)} sessions in [{args.start}, {args.end}] "
          f"(limit={args.limit}) -> {HIST_CACHE_DIR}", flush=True)

    if args.dry_run:
        print("[bflow-hist] DRY-RUN: no fetch, no write.", flush=True)
        return 0

    from research.bflow.minbar_cache import get_session_bars

    done = 0
    for session in sessions:
        bars_map = get_session_bars(session, tickers,
                                    cache_dir=HIST_CACHE_DIR)
        total = sum(len(b) for b in bars_map.values())
        zero = sum(1 for b in bars_map.values() if not b)
        done += 1
        print(f"[bflow-hist] {session}: {len(bars_map)} tickers, "
              f"{total} bar rows, {zero} zero-bar "
              f"({done}/{len(sessions)})", flush=True)

    print(f"[bflow-hist] COMPLETE: {done} sessions.", flush=True)
    return 0


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main(sys.argv[1:]))
