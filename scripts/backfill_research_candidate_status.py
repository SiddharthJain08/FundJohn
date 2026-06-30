#!/usr/bin/env python3
"""
backfill_research_candidate_status.py — one-time idempotent backfill that
reclassifies research_candidates rows that were hunted (hunter_result_json IS
NOT NULL) but whose status was never stamped by the finisher (status='pending').

Reclassification rules (CASE precedence, top-down):
  - rejection_reason_if_any present                       -> 'blocked_rejected'
  - data_tier='A' AND strategy_id registered (coded ok)   -> 'done'
  - data_tier='B'                                         -> 'blocked_buildable'
  - otherwise (Tier-A that never registered, Tier-C,
    no-tier)                                              -> 'blocked_unclassified'

The Tier-A 'done' is gated on strategy_registry membership: the live finisher only
stamps 'done' when coding actually promoted the strategy (outcome.promoted), so a
Tier-A row whose strategy_id never registered was attempted-but-failed (or never
coded) and is NOT done — marking it 'done' would lie. On the live book ~45% of
hunted-pending Tier-A rows are unregistered, so this gate is material, not cosmetic.
These fall through to 'blocked_unclassified' (terminal: hunted, tiered, no usable
strategy). Stamping is safe for the _hunt re-hunt guard (already excluded by
hunter_result_json IS NOT NULL) and does NOT gate finisher re-tiering (window-based).

Rows where hunter_result_json IS NULL (never hunted) stay 'pending'.
Rows already in a terminal status are untouched (WHERE status='pending').
Idempotent: re-running produces the same result.

APPLY-ONLY: run this script manually as a deploy step; it is never auto-run.
Test: tests/test_backfill_research_candidate_status.py (temp table + rollback).

Usage:
  python3 scripts/backfill_research_candidate_status.py
  POSTGRES_URI=... python3 scripts/backfill_research_candidate_status.py
"""

import os
import sys

try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not installed", file=sys.stderr)
    sys.exit(1)

RECLASSIFY = """
UPDATE research_candidates rc SET status = CASE
    WHEN hunter_result_json->>'rejection_reason_if_any' IS NOT NULL THEN 'blocked_rejected'
    WHEN data_tier = 'A' AND EXISTS (
           SELECT 1 FROM strategy_registry sr
            WHERE sr.id = rc.hunter_result_json->>'strategy_id') THEN 'done'
    WHEN data_tier = 'B' THEN 'blocked_buildable'
    ELSE 'blocked_unclassified' END
  WHERE status='pending' AND hunter_result_json IS NOT NULL
    AND hunter_result_json::text NOT IN ('null','{}')
"""

HISTOGRAM = """
SELECT status, count(*) AS n
  FROM research_candidates
 GROUP BY 1
 ORDER BY 1
"""


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

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        cur = conn.cursor()

        # Before histogram.
        cur.execute(HISTOGRAM)
        before = dict(cur.fetchall())
        print("Before status histogram:")
        for status, n in sorted(before.items()):
            print(f"  {status}: {n}")

        # Apply reclassification.
        cur.execute(RECLASSIFY)
        updated = cur.rowcount
        conn.commit()
        print(f"\nUpdated {updated} rows.")

        # After histogram.
        cur.execute(HISTOGRAM)
        after = dict(cur.fetchall())
        print("\nAfter status histogram:")
        for status, n in sorted(after.items()):
            print(f"  {status}: {n}")

    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
