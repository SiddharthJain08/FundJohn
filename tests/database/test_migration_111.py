# tests/test_migration_111.py
import os
import psycopg2
import pytest

# Integration: needs live Postgres + POSTGRES_URI in the environment.
# (Previously "worked" unmarked only because another test module's import-time
# load_dotenv leaked the production .env into the test process.)
pytestmark = pytest.mark.integration

DSN = os.environ.get("POSTGRES_URI", "")

def test_table_exists():
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT to_regclass('public.ticker_metadata_snapshots')")
        assert cur.fetchone()[0] == "ticker_metadata_snapshots"

def test_pk_and_indexes():
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute("""
            SELECT indexname FROM pg_indexes
            WHERE tablename='ticker_metadata_snapshots' ORDER BY 1
        """)
        names = [r[0] for r in cur.fetchall()]
        assert "ticker_metadata_snapshots_pkey" in names
        assert "idx_meta_snapshots_symbol_date" in names
        assert "idx_meta_snapshots_date_active" in names

def test_idempotent_upsert():
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO ticker_metadata_snapshots
              (snapshot_date, symbol, asset_class, status, source_tag)
            VALUES ('2026-05-22', 'AAPL', 'us_equity', 'active', 'test')
            ON CONFLICT (snapshot_date, symbol) DO UPDATE SET source_tag = EXCLUDED.source_tag
        """)
        cur.execute("""
            INSERT INTO ticker_metadata_snapshots
              (snapshot_date, symbol, asset_class, status, source_tag)
            VALUES ('2026-05-22', 'AAPL', 'us_equity', 'active', 'test_v2')
            ON CONFLICT (snapshot_date, symbol) DO UPDATE SET source_tag = EXCLUDED.source_tag
        """)
        cur.execute("SELECT source_tag FROM ticker_metadata_snapshots WHERE symbol='AAPL' AND snapshot_date='2026-05-22'")
        assert cur.fetchone()[0] == "test_v2"
        cur.execute("DELETE FROM ticker_metadata_snapshots WHERE source_tag IN ('test', 'test_v2')")
