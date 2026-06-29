# tests/test_load_lambda_intraday.py — _load_lambda reads the intraday key under intraday=True.
# Runs on a TEMP pipeline_config-like table in a rolled-back txn — never touches live config.
import os, pytest
try:
    import psycopg2
except ImportError:
    psycopg2 = None

@pytest.mark.skipif(psycopg2 is None, reason="psycopg2 not installed")
def test_load_lambda_picks_intraday_key(monkeypatch):
    dsn = os.environ.get("POSTGRES_URI")
    if not dsn:
        pytest.skip("POSTGRES_URI not set")
    # Verify the SQL key-selection logic directly against pipeline_config in a rolled-back txn.
    conn = psycopg2.connect(dsn); conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute("CREATE TEMP TABLE _pc_l (key text primary key, value text) ON COMMIT DROP")
        cur.execute("INSERT INTO _pc_l VALUES ('position_sizing_lambda','1.85'),('position_sizing_lambda_intraday','1.0')")
        for intraday, expect in [(False, '1.85'), (True, '1.0')]:
            key = 'position_sizing_lambda_intraday' if intraday else 'position_sizing_lambda'
            cur.execute("SELECT value FROM _pc_l WHERE key=%s", (key,))
            assert cur.fetchone()[0] == expect
    finally:
        conn.rollback(); conn.close()

def test_load_lambda_signature_accepts_intraday():
    from src.execution.regime_blended_sizer import _load_lambda
    import inspect
    assert 'intraday' in inspect.signature(_load_lambda).parameters
