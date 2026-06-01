#!/usr/bin/env python3
"""SP-5.1b-ii EOD step: emit option delta-hedge targets. Gated OPENCLAW_OPTION_DELTA_HEDGE.
Runs after `signals` in the 4PM eod-signal-register cycle. Honors --dry-run.
"""
import os, sys, argparse, datetime as dt
from pathlib import Path

# Insert src/ so `from execution.option_hedge import ...` resolves.
# Matches the convention used by tests/test_option_hedge.py and the
# src/execution/ module structure. PYTHONPATH (set by daily_cycle_node.js)
# also includes both ROOT and ROOT/src, but this guards standalone invocation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    # --date is passed by resolveScript (daily_cycle_node.js → resolve_script.js line 34)
    ap.add_argument('--date', default=dt.date.today().isoformat())
    args = ap.parse_args()

    if os.environ.get('OPENCLAW_OPTION_DELTA_HEDGE') != '1':
        print('[option-hedge] gate OFF — skip')
        return 0

    as_of = dt.date.fromisoformat(args.date)

    import psycopg2
    from execution.option_hedge import compute_option_hedge_targets

    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    cur = conn.cursor()
    try:
        compute_option_hedge_targets(cur, as_of=as_of)
        if args.dry_run:
            conn.rollback()
            print('[option-hedge] dry-run rolled back')
        else:
            conn.commit()
            print('[option-hedge] hedge targets written')
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
