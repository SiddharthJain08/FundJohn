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


def test_compute_structure_delta_sums_leg_deltas():
    from execution.option_hedge import compute_structure_delta
    from unittest.mock import patch
    legs = [{'occ':'SPY260626C00759000','right':'call','strike':759.0,'expiry':'2026-06-26'},
            {'occ':'SPY260626P00759000','right':'put','strike':759.0,'expiry':'2026-06-26'}]
    def _fake_leg_delta(occ, right, strike, expiry):
        return 0.52 if right == 'call' else -0.48
    with patch('execution.option_hedge._leg_delta', side_effect=_fake_leg_delta):
        net = compute_structure_delta(legs, contracts=2)
    assert round(net, 1) == round((0.52 - 0.48) * 100 * 2, 1)   # +8.0

def test_compute_structure_delta_none_when_a_leg_delta_missing():
    from execution.option_hedge import compute_structure_delta
    from unittest.mock import patch
    legs = [{'occ':'C','right':'call','strike':1.0,'expiry':'2026-06-26'},
            {'occ':'P','right':'put','strike':1.0,'expiry':'2026-06-26'}]
    with patch('execution.option_hedge._leg_delta', side_effect=[0.5, None]):
        assert compute_structure_delta(legs, contracts=1) is None  # fail-closed


def test_compute_option_hedge_targets_writes_APPROVED_tagged_row():
    from execution.option_hedge import compute_option_hedge_targets
    import datetime as dt
    from unittest.mock import patch
    inserts = []
    class _Cur:
        description = [('option_strategy_id',),('underlying',),('structure_legs',),
                       ('contracts',),('current_hedge_qty',),('target_hedge_qty',)]
        def execute(self, sql, params=None):
            if 'INSERT INTO execution_signals' in sql or 'option_hedge_ledger' in sql:
                inserts.append((sql, params))
        def fetchall(self):
            return [('S_strad','SPY',[{'occ':'SPY260626C00759000','right':'call','strike':759.0,'expiry':'2026-06-26'}],2,0.0,None)]
    with patch('execution.option_hedge.compute_structure_delta', return_value=8.0):
        compute_option_hedge_targets(_Cur(), as_of=dt.date(2026,6,1))
    sig = [(s,p) for (s,p) in inserts if 'execution_signals' in s]
    assert sig, 'no execution_signals hedge row'
    s, p = sig[0]
    assert 'APPROVED' in str(p) or 'APPROVED' in s        # written directly APPROVED (gate-bypass)
    assert any('is_hedge' in str(x) for x in p)            # tagged
    assert any('hedge_shares' in str(x) for x in p)        # carries fixed share target
    # net +8 -> target_shares -8 -> direction SHORT
    assert 'SHORT' in str(p)
    led = [(s,p) for (s,p) in inserts if 'INSERT INTO option_hedge_ledger' in s]
    assert led and -8.0 in led[0][1]                       # ledger target = -net

def test_compute_option_hedge_targets_skips_on_none_delta():
    from execution.option_hedge import compute_option_hedge_targets
    import datetime as dt
    from unittest.mock import patch
    inserts = []
    class _Cur:
        description=[('option_strategy_id',),('underlying',),('structure_legs',),('contracts',),('current_hedge_qty',),('target_hedge_qty',)]
        def execute(self, sql, params=None):
            if 'execution_signals' in sql: inserts.append(sql)
        def fetchall(self): return [('S','SPY',[{'occ':'X','right':'call','strike':1,'expiry':'2026-06-26'}],1,0.0,None)]
    with patch('execution.option_hedge.compute_structure_delta', return_value=None):
        compute_option_hedge_targets(_Cur(), as_of=dt.date(2026,6,1))
    assert not inserts  # fail-closed: no hedge row when delta unresolved
