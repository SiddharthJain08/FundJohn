import os, pytest
try:
    import psycopg2
except ImportError:
    psycopg2 = None
PG = os.environ.get('POSTGRES_URI')

@pytest.mark.skipif(not (PG and psycopg2), reason='no POSTGRES_URI/psycopg2')
def test_option_hedge_ledger_table_exists():
    conn = psycopg2.connect(PG); cur = conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='option_hedge_ledger'")
    cols = {r[0] for r in cur.fetchall()}
    assert {'option_strategy_id','underlying','structure_legs','contracts',
            'current_hedge_qty','target_hedge_qty','last_rehedge_date','status'} <= cols
    conn.close()
