#!/usr/bin/env python3
"""SP-7 Phase B B4 — 12th-Saturday ladder sentinel.

Runs inside weekend_saturday.sh step 8 (the slot the legacy universe-recs
invocation vacated). If ≥12 weeks (84 days) since the last FULL ladder run
(redis sp7:ladder:last_full_run), seed a full run + arm the nightly window.
Compute happens in the following nightly windows, NEVER on Saturday.

Usage: python3 scripts/check_ladder_saturday.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEEKS_12_DAYS = 84
KEY = 'sp7:ladder:last_full_run'


def is_due(last_iso: str | None, *, today: date) -> bool:
    if not last_iso:
        return True
    try:
        last = date.fromisoformat(last_iso)
    except ValueError:
        return True
    return (today - last).days >= WEEKS_12_DAYS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    last = None
    try:
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL',
                                          'redis://localhost:6379'),
                           socket_connect_timeout=3, decode_responses=True)
        last = r.get(KEY)
    except Exception as e:
        print(f'[ladder-saturday] redis unavailable ({e}) — treating as due')
    due = is_due(last, today=date.today())
    print(f'[ladder-saturday] last_full_run={last} due={due}')
    if not due or args.dry_run:
        return 0
    rc = subprocess.run(
        ['python3', 'scripts/run_universe_ladder.py', 'seed', '--arm'],
        cwd=str(ROOT)).returncode
    print(f'[ladder-saturday] seed rc={rc}')
    return rc


if __name__ == '__main__':
    sys.exit(main())
