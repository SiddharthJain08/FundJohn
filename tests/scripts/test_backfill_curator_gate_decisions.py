"""
tests/test_backfill_curator_gate_decisions.py — the backfill flips historical curator
gate_decisions for implementable_candidate papers from 'reject' to 'pass'. Runs on a TEMP
table inside a rolled-back txn — never touches live paper_gate_decisions.

SCHEMA NOTE (confirmed from 033_paper_gate_decisions.sql): paper_gate_decisions has NO
'predicted_bucket' column. The bucket is stored in reason_code as 'bucket_<bucket_name>'.
So the backfill targets reason_code='bucket_implementable_candidate', not predicted_bucket.
"""
import os
import pytest

try:
    import psycopg2
except ImportError:
    psycopg2 = None

# Matches the SQL in backfill_curator_gate_decisions.py — uses reason_code, not predicted_bucket.
BACKFILL_SQL = (
    "UPDATE {t} SET outcome='pass' "
    "WHERE gate_name='curator' AND outcome='reject' AND reason_code='bucket_implementable_candidate'"
)


@pytest.mark.skipif(psycopg2 is None, reason="psycopg2 not installed")
def test_backfill_flips_only_implementable_reject_rows():
    dsn = os.environ.get("POSTGRES_URI")
    if not dsn:
        pytest.skip("POSTGRES_URI not set")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        cur = conn.cursor()
        # Temp table mirrors the real paper_gate_decisions columns used by the backfill.
        cur.execute(
            "CREATE TEMP TABLE _pgd (gate_name text, outcome text, reason_code text) ON COMMIT DROP"
        )
        cur.execute(
            "INSERT INTO _pgd VALUES "
            "('curator','reject','bucket_implementable_candidate'),"  # -> flip to pass
            "('curator','reject','bucket_low'),"                       # stays reject
            "('curator','pass','bucket_high'),"                        # already pass — untouched
            "('hunter','reject','bucket_implementable_candidate')"     # not curator gate -> untouched
        )
        cur.execute(BACKFILL_SQL.format(t="_pgd"))
        cur.execute("SELECT outcome, reason_code, gate_name FROM _pgd ORDER BY 1,2,3")
        got = cur.fetchall()
        # implementable_candidate curator row flipped
        assert ('pass', 'bucket_implementable_candidate', 'curator') in got, f"Expected flip: {got}"
        # low bucket stays reject
        assert ('reject', 'bucket_low', 'curator') in got, f"Expected reject low: {got}"
        # high bucket was already pass — untouched
        assert ('pass', 'bucket_high', 'curator') in got, f"Expected pass high: {got}"
        # hunter gate implementable_candidate untouched
        assert ('reject', 'bucket_implementable_candidate', 'hunter') in got, f"Expected hunter untouched: {got}"
    finally:
        conn.rollback()
        conn.close()
