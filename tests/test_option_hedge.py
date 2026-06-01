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


def test_upsert_hedge_target_writes_target_and_legs():
    from execution.option_hedge import upsert_hedge_target
    captured = []
    class _Cur:
        def execute(self, sql, params): captured.append((sql, params))
    upsert_hedge_target(_Cur(), strategy_id='S_strad', underlying='SPY',
        legs=[{'occ':'SPY260626C00759000','right':'call','strike':759.0}],
        contracts=2, target_hedge_qty=-150.0, as_of='2026-06-01')
    sql, params = captured[-1]
    assert 'option_hedge_ledger' in sql and 'ON CONFLICT' in sql
    assert 'S_strad' in params and 'SPY' in params and -150.0 in params

def test_load_active_hedges_returns_rows():
    from execution.option_hedge import load_active_hedges
    class _Cur:
        description = [('option_strategy_id',),('underlying',),('structure_legs',),
                       ('contracts',),('current_hedge_qty',),('target_hedge_qty',)]
        def execute(self, sql, params=None): self._sql = sql
        def fetchall(self): return [('S_strad','SPY',[{'occ':'X'}],2,-100.0,-150.0)]
    rows = load_active_hedges(_Cur())
    assert rows[0]['option_strategy_id'] == 'S_strad' and rows[0]['underlying'] == 'SPY'
    assert rows[0]['current_hedge_qty'] == -100.0

def test_hedge_qty_by_underlying_sums():
    from execution.option_hedge import hedge_qty_by_underlying
    class _Cur:
        def execute(self, sql, params=None): self._sql = sql
        def fetchall(self): return [('SPY', -100.0), ('IWM', 50.0)]
    out = hedge_qty_by_underlying(_Cur())
    assert out == {'SPY': -100.0, 'IWM': 50.0}

def test_close_hedge_marks_closed_and_zeros_target():
    from execution.option_hedge import close_hedge
    captured = []
    class _Cur:
        def execute(self, sql, params): captured.append((sql, params))
    close_hedge(_Cur(), 'S_strad', 'SPY')
    sql, params = captured[-1]
    assert "status='closed'" in sql and 'target_hedge_qty=0' in sql
    assert 'S_strad' in params and 'SPY' in params
