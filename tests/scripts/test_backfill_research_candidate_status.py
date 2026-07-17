import os
import pytest

try:
    import psycopg2
except ImportError:
    psycopg2 = None

# The reclassify the backfill applies, with table names substituted for the temp test.
# {t} = research_candidates, {sr} = strategy_registry (the Tier-A 'done' gate).
RECLASSIFY = """
UPDATE {t} rc SET status = CASE
    WHEN hunter_result_json->>'rejection_reason_if_any' IS NOT NULL THEN 'blocked_rejected'
    WHEN data_tier = 'A' AND EXISTS (
           SELECT 1 FROM {sr} sr
            WHERE sr.id = rc.hunter_result_json->>'strategy_id') THEN 'done'
    WHEN data_tier = 'B' THEN 'blocked_buildable'
    ELSE 'blocked_unclassified' END
  WHERE status='pending' AND hunter_result_json IS NOT NULL
    AND hunter_result_json::text NOT IN ('null','{{}}')
"""


@pytest.mark.skipif(psycopg2 is None, reason="psycopg2 not installed")
def test_backfill_reclassifies_hunted_pending():
    dsn = os.environ.get("POSTGRES_URI")
    if not dsn:
        pytest.skip("POSTGRES_URI not set")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("CREATE TEMP TABLE _sr (id text) ON COMMIT DROP")
        cur.execute("INSERT INTO _sr VALUES ('x_reg')")  # only the registered Tier-A strategy
        cur.execute(
            "CREATE TEMP TABLE _rc (status text, data_tier text, hunter_result_json jsonb) ON COMMIT DROP"
        )
        cur.execute(
            """INSERT INTO _rc VALUES
            ('pending','A','{"strategy_id":"x_reg"}'),
            ('pending','A','{"strategy_id":"x_unreg"}'),
            ('pending','B','{"strategy_id":"y"}'),
            ('pending',NULL,'{"rejection_reason_if_any":"no_data"}'),
            ('pending',NULL,NULL),
            ('pending','C','{"strategy_id":"c"}'),
            ('done','A','{"strategy_id":"z"}')"""
        )
        cur.execute(RECLASSIFY.format(t="_rc", sr="_sr"))
        cur.execute("SELECT status,count(*) FROM _rc GROUP BY 1 ORDER BY 1")
        got = dict(cur.fetchall())
        # registered Tier-A -> done (+ the pre-existing 'done' row, untouched)
        assert got.get('done') == 2
        # unregistered Tier-A (failed coding) + genuine Tier-C -> blocked_unclassified (NOT done)
        assert got.get('blocked_unclassified') == 2
        assert got.get('blocked_buildable') == 1
        assert got.get('blocked_rejected') == 1
        # the hunter_result_json NULL row stays pending (un-hunted)
        assert got.get('pending') == 1
    finally:
        conn.rollback()
        conn.close()
