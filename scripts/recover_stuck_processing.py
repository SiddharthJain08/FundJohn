#!/usr/bin/env python3
"""
recover_stuck_processing.py — idempotent, APPLY-only script that resets
research_candidates rows stuck in status='processing' back to 'pending'.

A row is eligible for reset only if ALL of:
  - status = 'processing'
  - submitted_at < NOW() - INTERVAL '<timeout> minutes'   (timed out)
  - hunter_result_json IS NULL                             (not yet hunted)

The hunter_result_json guard is critical: a row that already has a hunter
result is actively in the finisher pipeline (claim→hunt→finisher); resetting
it would corrupt in-flight work.  Only rows where the claim crashed mid-flight
(hunt never started) are eligible.

Idempotent: a second run resets 0 rows (the just-reset rows are now 'pending'
and fresh, so they don't satisfy the timeout predicate either).

APPLY-ONLY: run manually as a deploy step or one-off cron; never auto-run.
Test: tests/test_recover_stuck_processing.py (temp table + rollback).

Usage:
  python3 scripts/recover_stuck_processing.py
  POSTGRES_URI=... python3 scripts/recover_stuck_processing.py
  RESEARCH_CANDIDATE_PROCESSING_TIMEOUT_MIN=60 python3 scripts/recover_stuck_processing.py
"""

import os
import sys

try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not installed", file=sys.stderr)
    sys.exit(1)

# Default timeout: rows stuck in 'processing' longer than this are eligible.
DEFAULT_TIMEOUT_MIN = 30

RESET_STUCK = """
UPDATE research_candidates
   SET status = 'pending'
 WHERE status = 'processing'
   AND submitted_at < NOW() - (INTERVAL '1 minute' * %(timeout_min)s)
   AND hunter_result_json IS NULL
"""

COUNT_PROCESSING = "SELECT COUNT(*) FROM research_candidates WHERE status = 'processing'"


def main():
    # Load .env if POSTGRES_URI not already set.
    if not os.environ.get("POSTGRES_URI"):
        env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        k = k.strip()
                        if k and k == 'POSTGRES_URI':
                            os.environ.setdefault(k, v.strip())
        except FileNotFoundError:
            pass

    dsn = os.environ.get("POSTGRES_URI")
    if not dsn:
        print("ERROR: POSTGRES_URI not set", file=sys.stderr)
        sys.exit(1)

    timeout_min = int(
        os.environ.get("RESEARCH_CANDIDATE_PROCESSING_TIMEOUT_MIN", DEFAULT_TIMEOUT_MIN)
    )
    print(f"Processing-stuck recovery — timeout={timeout_min} min, hunter_result_json IS NULL guard ON")

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        cur = conn.cursor()

        # Before count.
        cur.execute(COUNT_PROCESSING)
        before = cur.fetchone()[0]
        print(f"\nBefore: {before} row(s) in status='processing'")

        # Apply reset.
        cur.execute(RESET_STUCK, {"timeout_min": timeout_min})
        updated = cur.rowcount
        conn.commit()
        print(f"Reset {updated} stuck row(s) to 'pending'.")

        # After count.
        cur.execute(COUNT_PROCESSING)
        after = cur.fetchone()[0]
        print(f"After:  {after} row(s) in status='processing'")

    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
