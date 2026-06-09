#!/usr/bin/env python3
"""
strategy_backtest_metrics.py — run a single backtest and print its metrics as
JSON. Backs the gated-apply loop in mastermind_code_review.js, which needs to
evaluate a proposed fix BEFORE persisting it (commit=False) and only persist
(commit=True → panel rebuild) if it doesn't regress.

The unified_backtest CLI always commits and prints no machine-readable metrics;
this wrapper exposes run_backtest(commit=<bool>, return_metrics=True).

Imports the backtest engine from $OPENCLAW_DIR/src so the engine's ROOT (and
therefore data/master/*.parquet) resolves to the LIVE checkout even when the
--strategy-file under test lives in a worktree.

Usage:
  OPENCLAW_DIR=/root/openclaw python3 scripts/strategy_backtest_metrics.py \
      --strategy-file <path.py> [--commit] [--start-date YYYY-MM-DD]
Prints: {"run_id","sharpe","trades","max_dd_pct","return_pct"} on stdout.
"""
import os
import sys
import json
import argparse
from pathlib import Path

OPENCLAW_DIR = os.environ.get('OPENCLAW_DIR') or str(Path(__file__).resolve().parents[1])
sys.path.insert(0, os.path.join(OPENCLAW_DIR, 'src'))

from backtest.unified_backtest import run_backtest, _resolve_instrument_class  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--strategy-file', required=True)
    ap.add_argument('--commit', action='store_true',
                    help='persist the run + rebuild the dashboard panel (default: ephemeral)')
    ap.add_argument('--start-date', default=None)
    args = ap.parse_args()

    sid = Path(args.strategy_file).stem
    kwargs = dict(
        filepath=args.strategy_file,
        commit=args.commit,
        return_metrics=True,
        instrument_class=_resolve_instrument_class(sid, filepath=args.strategy_file),
    )
    if args.start_date:
        kwargs['start_date'] = args.start_date

    run_id, m = run_backtest(sid, **kwargs)
    print(json.dumps({
        'run_id':     str(run_id),
        'sharpe':     m.get('sharpe'),
        'trades':     m.get('total_trades'),
        'max_dd_pct': m.get('max_dd_pct'),
        'return_pct': m.get('return_pct'),
        'committed':  bool(args.commit),
    }))
    return 0


if __name__ == '__main__':
    sys.exit(main())
