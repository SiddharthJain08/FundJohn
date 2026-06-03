"""SP-5.1c Task 7 — TDD: option_hedge_ledger structural row written on delta-hedged fill.

Red → green: upsert_hedge_ledger_on_fill must INSERT the ledger row (idempotent
ON CONFLICT) when a delta-hedged structure fills. Written before the implementation.
"""
from execution.option_hedge import upsert_hedge_ledger_on_fill


class _Cur:
    def __init__(self): self.calls = []
    def execute(self, sql, params=None): self.calls.append((sql, params))


def test_writes_ledger_row_with_legs_and_contracts():
    cur = _Cur()
    legs = [{'occ': 'SPY260718C00500000', 'right': 'call', 'strike': 500.0, 'expiry': '2026-07-18'},
            {'occ': 'SPY260718P00500000', 'right': 'put',  'strike': 500.0, 'expiry': '2026-07-18'}]
    upsert_hedge_ledger_on_fill(cur, option_strategy_id='S_long_straddle_delta_hedged',
                                underlying='SPY', legs=legs, contracts=2)
    assert cur.calls, 'no SQL executed'
    sql, params = cur.calls[0]
    assert 'option_hedge_ledger' in sql and 'ON CONFLICT' in sql
    assert any('SPY260718C00500000' in str(p) for p in (params or ()))


def test_idempotent_on_conflict():
    """ON CONFLICT path must update structure_legs + contracts but leave target_hedge_qty alone."""
    cur = _Cur()
    legs = [{'occ': 'SPY260718C00500000', 'right': 'call', 'strike': 500.0, 'expiry': '2026-07-18'}]
    upsert_hedge_ledger_on_fill(cur, option_strategy_id='S_single_leg_delta',
                                underlying='SPY', legs=legs, contracts=1)
    assert len(cur.calls) == 1
    sql, _ = cur.calls[0]
    # The DO UPDATE must NOT overwrite target_hedge_qty (EOD step owns that column)
    assert 'target_hedge_qty' not in sql.split('DO UPDATE')[1]


def test_status_set_to_active():
    cur = _Cur()
    legs = [{'occ': 'SPY260718C00500000', 'right': 'call', 'strike': 500.0, 'expiry': '2026-07-18'}]
    upsert_hedge_ledger_on_fill(cur, option_strategy_id='S_test',
                                underlying='SPY', legs=legs, contracts=3)
    sql, params = cur.calls[0]
    assert "'active'" in sql
    # contracts value passed through
    assert 3 in (params or ())
