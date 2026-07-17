# tests/test_migration_139.py — migration 139 dedups byte-identical duplicate pipeline_config
# `key` rows and restores a unique key. Runs on a TEMP table inside a rolled-back txn — live
# pipeline_config is never read or written.
import os
import pytest

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None

DEDUP = "DELETE FROM {t} a USING {t} b WHERE a.ctid > b.ctid AND a.key = b.key;"

@pytest.mark.skipif(psycopg2 is None, reason="psycopg2 not installed")
def test_migration_139_dedup_and_restore():
    dsn = os.environ.get("POSTGRES_URI")
    if not dsn:
        pytest.skip("POSTGRES_URI not set")
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        # TEMP table mirroring pipeline_config WITHOUT the unique key, so we can seed the
        # corrupt-PK state (duplicate keys). ON COMMIT DROP + final rollback = zero residue.
        cur.execute("CREATE TEMP TABLE _pc139 (key text, value text, description text, "
                    "updated_at timestamptz) ON COMMIT DROP")
        cur.execute("INSERT INTO _pc139 (key, value) VALUES "
                    "('collection_enabled','true'),('collection_enabled','true'),"
                    "('collect_technicals','true'),('collect_technicals','true'),"
                    "('position_sizing_lambda','1.85')")
        cur.execute("SELECT COUNT(*) FROM _pc139")
        assert cur.fetchone()[0] == 5
        # run the migration's dedup statement
        cur.execute(DEDUP.format(t="_pc139"))
        cur.execute("SELECT key, COUNT(*) FROM _pc139 GROUP BY key HAVING COUNT(*) > 1")
        assert cur.fetchall() == [], "no duplicate keys remain after dedup"
        cur.execute("SELECT COUNT(*) FROM _pc139")
        assert cur.fetchone()[0] == 3
        # the unique key is now restorable — proves dedup made the table constraint-valid
        cur.execute("ALTER TABLE _pc139 ADD PRIMARY KEY (key)")
        # idempotent: re-running dedup changes nothing
        cur.execute(DEDUP.format(t="_pc139"))
        cur.execute("SELECT COUNT(*) FROM _pc139")
        assert cur.fetchone()[0] == 3
    finally:
        conn.rollback()   # discard temp table + all work; live DB untouched
        conn.close()
