"""
Tests for scripts/recover_stuck_processing.py

Uses a temp table (_rc) and rolls back so the live DB is never touched.
The UPDATE logic is inlined here (with table name substituted) to match
what the script applies to the real research_candidates table.

Mirror of tests/test_backfill_research_candidate_status.py.
"""
import os

import pytest

try:
    import psycopg2
except ImportError:
    psycopg2 = None

# The UPDATE that recover_stuck_processing.py applies, with {t} substituted
# for the temp table name during tests.
RESET_STUCK = """
UPDATE {t}
   SET status = 'pending'
 WHERE status = 'processing'
   AND submitted_at < NOW() - (INTERVAL '1 minute' * %(timeout_min)s)
   AND hunter_result_json IS NULL
"""


@pytest.mark.skipif(psycopg2 is None, reason="psycopg2 not installed")
def test_recover_stuck_processing():
    dsn = os.environ.get("POSTGRES_URI")
    if not dsn:
        pytest.skip("POSTGRES_URI not set")

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(
            """CREATE TEMP TABLE _rc (
                status             TEXT,
                submitted_at       TIMESTAMPTZ,
                hunter_result_json JSONB
            ) ON COMMIT DROP"""
        )
        cur.execute(
            """INSERT INTO _rc (status, submitted_at, hunter_result_json) VALUES
            -- 1. Stuck row: processing, 1h old, no hunter result → RESET to pending
            ('processing', NOW() - INTERVAL '1 hour',  NULL),
            -- 2. Fresh processing row: submitted_at = now → stays processing
            ('processing', NOW(),                       NULL),
            -- 3. Processing row WITH hunter result: in-flight → stays processing
            ('processing', NOW() - INTERVAL '2 hours', '{"strategy_id":"x"}'),
            -- 4. Pending row: untouched
            ('pending',    NOW() - INTERVAL '3 hours', NULL)
            """
        )

        timeout_min = 30  # matches the script's DEFAULT_TIMEOUT_MIN
        cur.execute(RESET_STUCK.format(t="_rc"), {"timeout_min": timeout_min})

        cur.execute("SELECT status, COUNT(*) FROM _rc GROUP BY 1 ORDER BY 1")
        got = dict(cur.fetchall())

        # Row 1 (stuck, old, null json) → reset to pending
        # Row 4 (already pending) → stays pending
        # Total pending = 2
        assert got.get('pending') == 2, f"expected 2 pending, got {got}"

        # Row 2 (fresh processing, null json) → stays processing (not past timeout)
        # Row 3 (processing, old, WITH json) → stays processing (hunter_result_json IS NOT NULL)
        assert got.get('processing') == 2, f"expected 2 processing, got {got}"

    finally:
        conn.rollback()
        conn.close()
