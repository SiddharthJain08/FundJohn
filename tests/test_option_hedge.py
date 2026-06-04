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
    # Pass workspace_id explicitly — unit tests exercise the no-resolve path.
    # Real-DB resolution (the None path) is covered by test_resolve_uses_dictcursor_when_workspace_none.
    with patch('execution.option_hedge.compute_structure_delta', return_value=8.0):
        compute_option_hedge_targets(_Cur(), as_of=dt.date(2026,6,1), workspace_id='ws-test')
    sig = [(s,p) for (s,p) in inserts if 'execution_signals' in s]
    assert sig, 'no execution_signals hedge row'
    s, p = sig[0]
    assert 'APPROVED' in str(p) or 'APPROVED' in s        # written directly APPROVED (gate-bypass)
    assert any('is_hedge' in str(x) for x in p)            # tagged
    assert any('hedge_shares' in str(x) for x in p)        # carries fixed share target
    # net +8 -> target_shares -8 -> direction SHORT
    assert 'SHORT' in str(p)
    # workspace_id must be the explicitly-passed value (p[1] is workspace_id positional arg)
    assert p[1] == 'ws-test', f'expected explicit workspace_id, got {p[1]!r}'
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
    # Pass workspace_id explicitly to skip resolution and isolate the skip logic.
    with patch('execution.option_hedge.compute_structure_delta', return_value=None):
        compute_option_hedge_targets(_Cur(), as_of=dt.date(2026,6,1), workspace_id='ws-test')
    assert not inserts  # fail-closed: no hedge row when delta unresolved


def test_compute_option_hedge_targets_skips_zero_delta():
    """A perfectly delta-neutral structure (net delta == 0) must not emit a hedge row —
    it would produce a SHORT-0 signal that confuses the executor."""
    from execution.option_hedge import compute_option_hedge_targets
    import datetime as dt
    from unittest.mock import patch
    inserts = []
    class _Cur:
        description=[('option_strategy_id',),('underlying',),('structure_legs',),('contracts',),('current_hedge_qty',),('target_hedge_qty',)]
        def execute(self, sql, params=None):
            if 'execution_signals' in sql: inserts.append((sql, params))
        def fetchall(self): return [('S_neutral','SPY',[{'occ':'X','right':'call','strike':1,'expiry':'2026-06-26'}],1,0.0,None)]
    # Pass workspace_id explicitly to skip resolution and isolate the skip logic.
    with patch('execution.option_hedge.compute_structure_delta', return_value=0.0):
        compute_option_hedge_targets(_Cur(), as_of=dt.date(2026,6,1), workspace_id='ws-test')
    assert not inserts, 'zero-delta structure must not write an execution_signals row'


def test_resolve_uses_dictcursor_when_workspace_none():
    """When workspace_id=None, compute_option_hedge_targets must open a RealDictCursor
    on the connection and call resolve_workspace on THAT cursor — NOT on the plain cur
    passed by the EOD step. This test uses a plain cursor whose fetchone() returns a
    TUPLE (as real plain cursors do); resolution must succeed via the DictCursor path.
    Fails on the unfixed code (which calls resolve_workspace(cur,...) → row['id'] on a
    tuple → TypeError); passes after the fix."""
    from execution.option_hedge import compute_option_hedge_targets
    import datetime as dt
    from unittest.mock import patch, MagicMock
    inserts = []

    # Simulate a RealDictCursor context manager that returns a dict row
    _dict_cur = MagicMock()
    _dict_cur.__enter__ = lambda s: s
    _dict_cur.__exit__ = MagicMock(return_value=False)
    _dict_cur.fetchone.return_value = {'id': 'ws-uuid'}

    # Simulate a connection whose cursor(cursor_factory=...) returns the dict cursor
    _conn = MagicMock()
    _conn.cursor.return_value = _dict_cur

    class _PlainCur:
        """Mimics a plain psycopg2 cursor: fetchone returns a TUPLE."""
        description = [('option_strategy_id',), ('underlying',), ('structure_legs',),
                       ('contracts',), ('current_hedge_qty',), ('target_hedge_qty',)]
        connection = _conn

        def execute(self, sql, params=None):
            if 'INSERT INTO execution_signals' in sql or 'option_hedge_ledger' in sql:
                inserts.append((sql, params))

        def fetchall(self):
            return [('S_strad', 'SPY',
                     [{'occ': 'SPY260626C00759000', 'right': 'call',
                       'strike': 759.0, 'expiry': '2026-06-26'}],
                     2, 0.0, None)]

        def fetchone(self):
            # Plain cursor returns a tuple — index access only, NOT row['id']
            return ('plain-workspace-id',)

    with patch('execution.option_hedge.compute_structure_delta', return_value=8.0):
        compute_option_hedge_targets(_PlainCur(), as_of=dt.date(2026, 6, 1))
        # workspace_id=None (default) → must resolve via DictCursor, not plain cur

    sig = [(s, p) for (s, p) in inserts if 'execution_signals' in s]
    assert sig, 'no execution_signals row emitted'
    s, p = sig[0]
    # workspace_id is the second positional param (index 1)
    assert p[1] == 'ws-uuid', (
        f'expected DictCursor-resolved UUID "ws-uuid", got {p[1]!r}; '
        f'fix did not route resolution through RealDictCursor'
    )


import subprocess, sys
def test_hedge_step_gate_off_skips():
    env = {**os.environ}; env.pop('OPENCLAW_OPTION_DELTA_HEDGE', None)
    r = subprocess.run([sys.executable, 'src/pipeline/run_option_hedge_targets.py'],
                       capture_output=True, text=True, env=env, cwd=os.getcwd())
    assert r.returncode == 0 and 'gate OFF' in r.stdout


def test_hedge_step_gate_off_skips_with_date():
    """Confirm gate-off path also works when --date is passed (the real cycle path)."""
    env = {**os.environ}; env.pop('OPENCLAW_OPTION_DELTA_HEDGE', None)
    r = subprocess.run([sys.executable, 'src/pipeline/run_option_hedge_targets.py',
                        '--date', '2026-06-01'],
                       capture_output=True, text=True, env=env, cwd=os.getcwd())
    assert r.returncode == 0 and 'gate OFF' in r.stdout


# ── Task 8: doctor dependency check + option_hedge system_check ───────────


# Part A: doctor check — gate-dependency (no DB needed)

def test_doctor_delta_hedge_gate_off_passes():
    """OPENCLAW_OPTION_DELTA_HEDGE unset → doctor check returns severity='pass'."""
    import importlib
    import src.maintenance.doctor as doctor
    importlib.reload(doctor)
    env_bak = os.environ.copy()
    os.environ.pop('OPENCLAW_OPTION_DELTA_HEDGE', None)
    try:
        result = doctor.check_option_delta_hedge_deps()
        assert result['severity'] == 'pass', result
    finally:
        os.environ.clear(); os.environ.update(env_bak)


def test_doctor_delta_hedge_gate_on_eod_off_fails():
    """OPENCLAW_OPTION_DELTA_HEDGE=1 but EOD gates off → severity='fail'."""
    import importlib
    import src.maintenance.doctor as doctor
    importlib.reload(doctor)
    env_bak = os.environ.copy()
    os.environ['OPENCLAW_OPTION_DELTA_HEDGE'] = '1'
    os.environ.pop('OPENCLAW_EOD_SIGNAL_REGISTER', None)
    os.environ.pop('OPENCLAW_EOD_RECONCILE', None)
    try:
        result = doctor.check_option_delta_hedge_deps()
        assert result['severity'] == 'fail', result
        assert 'EOD' in result['detail']
    finally:
        os.environ.clear(); os.environ.update(env_bak)


def test_doctor_delta_hedge_gate_on_all_eod_on_passes():
    """All three gates ON → severity='pass'."""
    import importlib
    import src.maintenance.doctor as doctor
    importlib.reload(doctor)
    env_bak = os.environ.copy()
    os.environ['OPENCLAW_OPTION_DELTA_HEDGE'] = '1'
    os.environ['OPENCLAW_EOD_SIGNAL_REGISTER'] = '1'
    os.environ['OPENCLAW_EOD_RECONCILE'] = '1'
    try:
        result = doctor.check_option_delta_hedge_deps()
        assert result['severity'] == 'pass', result
    finally:
        os.environ.clear(); os.environ.update(env_bak)


# Part B: system_check option_hedge — gate-off SKIP + gate-on/EOD-off FAIL

def test_option_hedge_check_gate_off_skips():
    env = {**os.environ}; env.pop('OPENCLAW_OPTION_DELTA_HEDGE', None)
    r = subprocess.run([sys.executable, '-m', 'src.system_checks',
                        '--check', 'option_hedge', '--json'],
                       capture_output=True, text=True, env=env,
                       cwd=os.getcwd(), timeout=60)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    import json as _j
    d = _j.loads(r.stdout)
    st = d['results'][0]['status']
    assert st in ('SKIP', 'PASS'), d


def test_option_hedge_check_gate_on_no_eod_fails(monkeypatch):
    """Gate ON but EOD gates off → FAIL with 'EOD' in detail (in-process)."""
    monkeypatch.setenv('OPENCLAW_OPTION_DELTA_HEDGE', '1')
    monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
    monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
    from system_checks.checks.option_hedge import _option_hedge
    from system_checks.types import Status
    status, detail = _option_hedge()
    assert status == Status.FAIL
    assert 'EOD' in detail


# ===========================================================================
# SP-5 Phase 1b (G4c) — deactivate_hedge_ledger_for_underlying
# ===========================================================================

def test_deactivate_hedge_ledger_for_underlying_sql_shape():
    """Unit: marks matching ACTIVE rows closed (status='closed', flag pattern — never
    DELETE) for the underlying regardless of strategy_id; returns rowcount."""
    from execution.option_hedge import deactivate_hedge_ledger_for_underlying
    captured = []

    class _Cur:
        rowcount = 3
        def execute(self, sql, params): captured.append((sql, params))
    n = deactivate_hedge_ledger_for_underlying(_Cur(), 'SPY')
    assert n == 3
    sql, params = captured[-1]
    assert "status='closed'" in sql and 'DELETE' not in sql.upper()
    assert "status='active'" in sql or "status = 'active'" in sql  # only flips active rows
    assert 'SPY' in params


@pytest.mark.skipif(not (PG and psycopg2), reason='no POSTGRES_URI/psycopg2')
def test_deactivate_hedge_ledger_for_underlying_db_rollback():
    """DB-backed (auto-rollback): only ACTIVE rows for the matching underlying flip to
    closed; rows for OTHER underlyings and already-closed rows are untouched. Uses
    synthetic underlyings (ZZG4CU/ZZG4CV) so the rowcount is deterministic regardless
    of any real SPY/IWM ledger rows in the live DB."""
    from execution.option_hedge import deactivate_hedge_ledger_for_underlying
    conn = psycopg2.connect(PG)
    try:
        cur = conn.cursor()
        # Seed: 2 active ZZG4CU rows (different strategy_id), 1 active ZZG4CV row,
        # 1 closed ZZG4CU row.
        seed = [
            ('__test_sp5g4c_a', 'ZZG4CU', 'active'),
            ('__test_sp5g4c_b', 'ZZG4CU', 'active'),
            ('__test_sp5g4c_c', 'ZZG4CV', 'active'),
            ('__test_sp5g4c_d', 'ZZG4CU', 'closed'),
        ]
        for sid, und, st in seed:
            cur.execute(
                """INSERT INTO option_hedge_ledger
                     (option_strategy_id, underlying, structure_legs, contracts,
                      current_hedge_qty, target_hedge_qty, status)
                   VALUES (%s,%s,'[]'::jsonb,1,5,-5,%s)""",
                (sid, und, st))
        n = deactivate_hedge_ledger_for_underlying(cur, 'ZZG4CU')
        assert n == 2, 'exactly the 2 ACTIVE ZZG4CU rows should flip'
        cur.execute(
            "SELECT option_strategy_id, status, target_hedge_qty FROM option_hedge_ledger "
            "WHERE option_strategy_id LIKE '__test_sp5g4c_%' ORDER BY option_strategy_id")
        rows = {r[0]: (r[1], float(r[2])) for r in cur.fetchall()}
        assert rows['__test_sp5g4c_a'][0] == 'closed' and rows['__test_sp5g4c_a'][1] == 0.0
        assert rows['__test_sp5g4c_b'][0] == 'closed'
        assert rows['__test_sp5g4c_c'][0] == 'active'   # ZZG4CV untouched
        assert rows['__test_sp5g4c_d'][0] == 'closed'   # was already closed
    finally:
        conn.rollback()   # never persist test rows (append-only / flag-pattern discipline)
        conn.close()


def test_route_mleg_close_deactivates_ledger_on_success(monkeypatch):
    """_route_mleg_close calls the guarded hedge-deactivation after the per-leg close
    loop (success case), passing the underlying."""
    import execution.alpaca_executor as ex
    from strategies.base import OptionSpec
    spec = OptionSpec(underlying='SPY', structure='straddle', right='call')
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'option_spec': spec, 'close_only': True}
    monkeypatch.setattr(ex, '_held_option_legs',
                        lambda und: ['SPY260718C00500000', 'SPY260718P00500000'])
    monkeypatch.setattr(ex, '_run_alpaca_cli', lambda args, **kw: (True, {'status': 'accepted'}, None))
    seen = {}
    monkeypatch.setattr(ex, '_deactivate_hedge_for_underlying_guarded',
                        lambda und: seen.update(und=und) or 1)
    res = ex._route_mleg_close(order, spec, coid='mc1')
    assert res['status'] == 'submitted'
    assert seen.get('und') == 'SPY', 'deactivation must be called with the underlying'


def test_route_mleg_close_deactivation_failure_does_not_abort(monkeypatch):
    """A deactivation failure must NEVER abort the close — the result still returns.
    Exercises the REAL guarded wrapper _deactivate_hedge_for_underlying_guarded by making
    the underlying DB connect raise; the wrapper must swallow it (return 0) and the close
    must return 'submitted' normally."""
    import execution.alpaca_executor as ex
    from strategies.base import OptionSpec
    spec = OptionSpec(underlying='SPY', structure='straddle', right='call')
    order = {'ticker': 'SPY', 'instrument_class': 'option', 'direction': 'long',
             'option_spec': spec, 'close_only': True}
    monkeypatch.setattr(ex, '_held_option_legs', lambda und: ['SPY260718C00500000'])
    monkeypatch.setattr(ex, '_run_alpaca_cli', lambda args, **kw: (True, {'status': 'accepted'}, None))
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://invalid')

    def _boom(*a, **k):
        raise RuntimeError('db down')
    # Make the connection (inside the guarded wrapper) raise.
    monkeypatch.setattr(ex.psycopg2, 'connect', _boom)
    # Must not raise; close result returns normally despite the DB failure.
    res = ex._route_mleg_close(order, spec, coid='mc2')
    assert res['status'] == 'submitted'


def test_leg_delta_extracts_true_occ_root_not_alpha_chars():
    """H4 root cause (T9 live smoke 2026-06-04): the underlying was derived via
    ''.join(c for c in occ if c.isalpha()), which swallows the OCC right-letter
    ('SPY260626C00754000' -> 'SPYC', puts -> 'SPYP') so _spot_price could never
    resolve and EVERY leg delta fail-closed to None — the hedge-target producer
    was structurally unable to emit. The true root is everything before the
    fixed 15-char OCC tail (yymmdd + C/P + 8-digit strike)."""
    from execution.option_hedge import _leg_delta
    from unittest.mock import patch
    spot_calls, chain_calls = [], []

    def _fake_spot(sym):
        spot_calls.append(sym)
        return 754.1

    def _fake_chain(und, exp, right, spot, band_pct):
        chain_calls.append(und)
        # underlying-sensitive: only the TRUE root yields the leg's snapshot
        if und != 'SPY':
            return []
        occ = 'SPY260626C00754000' if right == 'call' else 'SPY260626P00754000'
        return [(754.0, 0.5123 if right == 'call' else -0.4877, occ)]

    with patch('execution.alpaca_executor._spot_price', side_effect=_fake_spot), \
         patch('execution.alpaca_executor._option_chain_greeks', side_effect=_fake_chain):
        d_call = _leg_delta('SPY260626C00754000', 'call', 754.0, '2026-06-26')
        d_put = _leg_delta('SPY260626P00754000', 'put', 754.0, '2026-06-26')

    assert spot_calls == ['SPY', 'SPY']     # NOT 'SPYC' / 'SPYP'
    assert chain_calls == ['SPY', 'SPY']
    assert d_call == 0.5123
    assert d_put == -0.4877
