import os
import psycopg2
import pytest

# Integration: needs live Postgres + POSTGRES_URI in the environment.
# (Previously "worked" unmarked only because another test module's import-time
# load_dotenv leaked the production .env into the test process.)
pytestmark = pytest.mark.integration

DSN = os.environ.get("POSTGRES_URI", "")

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
