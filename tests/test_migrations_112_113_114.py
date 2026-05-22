import os
import psycopg2

DSN = os.environ["POSTGRES_URI"]

def _exists(table):
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        result = cur.fetchone()[0]
        return result == table

def test_recs_table():
    assert _exists("strategy_universe_recommendations")

def test_audit_table():
    assert _exists("universe_resolution_audit")

def test_quarantine_table():
    assert _exists("data_quarantine")

def test_quarantine_lookup_index():
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute("""
            SELECT indexdef FROM pg_indexes
            WHERE tablename='data_quarantine' AND indexname='idx_quarantine_lookup'
        """)
        row = cur.fetchone()
        assert row is not None
        assert "superseded_at IS NULL" in row[0]
