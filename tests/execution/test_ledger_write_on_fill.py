"""SP-5.1c Task 7 — TDD: option_hedge_ledger structural row written on delta-hedged fill.

Red → green: upsert_hedge_ledger_on_fill must INSERT the ledger row (idempotent
ON CONFLICT) when a delta-hedged structure fills. Written before the implementation.

SP-5.1a (G2 residual): the upsert now also runs an advisory SELECT for ACTIVE ledger
rows on the same underlying under a DIFFERENT option_strategy_id and logs a loud
warning if any exist (it does NOT modify them). So a mock cursor needs fetchall().
"""
import logging

from execution.option_hedge import upsert_hedge_ledger_on_fill


class _Cur:
    """Mock cursor. `select_rows` is returned by fetchall() (the G2 advisory check)."""
    def __init__(self, select_rows=None):
        self.calls = []
        self._select_rows = select_rows or []
    def execute(self, sql, params=None):
        self.calls.append((sql, params))
    def fetchall(self):
        return list(self._select_rows)


def _insert_call(cur):
    """Return the INSERT INTO option_hedge_ledger call (skips the advisory SELECT)."""
    for sql, params in cur.calls:
        if 'INSERT INTO option_hedge_ledger' in sql:
            return sql, params
    raise AssertionError(f'no INSERT executed; calls={[c[0][:40] for c in cur.calls]}')


def test_writes_ledger_row_with_legs_and_contracts():
    cur = _Cur()
    legs = [{'occ': 'SPY260718C00500000', 'right': 'call', 'strike': 500.0, 'expiry': '2026-07-18'},
            {'occ': 'SPY260718P00500000', 'right': 'put',  'strike': 500.0, 'expiry': '2026-07-18'}]
    upsert_hedge_ledger_on_fill(cur, option_strategy_id='S_long_straddle_delta_hedged',
                                underlying='SPY', legs=legs, contracts=2)
    assert cur.calls, 'no SQL executed'
    sql, params = _insert_call(cur)
    assert 'option_hedge_ledger' in sql and 'ON CONFLICT' in sql
    assert any('SPY260718C00500000' in str(p) for p in (params or ()))


def test_idempotent_on_conflict():
    """ON CONFLICT path must update structure_legs + contracts but leave target_hedge_qty alone."""
    cur = _Cur()
    legs = [{'occ': 'SPY260718C00500000', 'right': 'call', 'strike': 500.0, 'expiry': '2026-07-18'}]
    upsert_hedge_ledger_on_fill(cur, option_strategy_id='S_single_leg_delta',
                                underlying='SPY', legs=legs, contracts=1)
    sql, _ = _insert_call(cur)
    # The DO UPDATE must NOT overwrite target_hedge_qty (EOD step owns that column)
    assert 'target_hedge_qty' not in sql.split('DO UPDATE')[1]


def test_status_set_to_active():
    cur = _Cur()
    legs = [{'occ': 'SPY260718C00500000', 'right': 'call', 'strike': 500.0, 'expiry': '2026-07-18'}]
    upsert_hedge_ledger_on_fill(cur, option_strategy_id='S_test',
                                underlying='SPY', legs=legs, contracts=3)
    sql, params = _insert_call(cur)
    assert "'active'" in sql
    # contracts value passed through
    assert 3 in (params or ())


def test_g2_warns_on_conflicting_active_strategy(caplog):
    """G2: an ACTIVE ledger row on the same underlying under a DIFFERENT
    option_strategy_id ⇒ a loud warning is logged (the prior row is NOT modified —
    only the INSERT for the new key is executed)."""
    cur = _Cur(select_rows=[('S_prior_straddle',)])  # existing active row, different sid
    legs = [{'occ': 'SPY260718C00500000', 'right': 'call', 'strike': 500.0, 'expiry': '2026-07-18'}]
    with caplog.at_level(logging.WARNING):
        upsert_hedge_ledger_on_fill(cur, option_strategy_id='S_new_strangle',
                                    underlying='SPY', legs=legs, contracts=1)
    assert any('DIFFERENT option_strategy_id' in r.message or 'different' in r.message.lower()
               for r in caplog.records if r.levelno >= logging.WARNING), \
        'a conflicting-active-strategy warning must be logged'
    # The new key is still inserted; no UPDATE/DELETE of the prior row.
    insert_sql, _ = _insert_call(cur)
    assert 'INSERT INTO option_hedge_ledger' in insert_sql
    assert not any('UPDATE option_hedge_ledger SET status' in s for s, _ in cur.calls)


def test_g2_no_warn_when_no_conflict(caplog):
    """No conflicting active row ⇒ no G2 warning."""
    cur = _Cur(select_rows=[])
    legs = [{'occ': 'SPY260718C00500000', 'right': 'call', 'strike': 500.0, 'expiry': '2026-07-18'}]
    with caplog.at_level(logging.WARNING):
        upsert_hedge_ledger_on_fill(cur, option_strategy_id='S_only',
                                    underlying='SPY', legs=legs, contracts=1)
    assert not any('DIFFERENT option_strategy_id' in r.message
                   for r in caplog.records if r.levelno >= logging.WARNING)
