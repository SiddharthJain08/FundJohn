"""Tests for proposal_manager — approve / reject / modify lifecycle."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import proposal_manager as pm  # noqa: E402


class FakeCursor:
    def __init__(self, rows_by_call=None):
        # rows_by_call: list of rows to return on successive fetchone calls
        self._queue = list(rows_by_call or [])
        self.executed: list = []
        self.rowcount: int = 0

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        self.rowcount = 0  # tests don't assert rowcount; just expose attribute
    def fetchone(self):
        return self._queue.pop(0) if self._queue else None
    def fetchall(self):
        return list(self._queue)


class FakeConn:
    def __init__(self, rows_by_call=None):
        self.cur = FakeCursor(rows_by_call)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def cursor(self): return self.cur
    def commit(self): self.committed = True


def _pending_row(pid=42, sid='s1', regime='LOW_VOL', proposed_eligible=False,
                 proposed_size=0.6):
    # Tuple matches the SELECT FOR UPDATE column order in proposal_manager
    return (pid, sid, regime, 'pending',
            proposed_eligible, proposed_size, None, None, None,
            0.75, 'test reason', None)


def test_approve_calls_set_params_and_marks_approved(monkeypatch):
    """The happy path: lock row, write to params, mark approved."""
    conn = FakeConn(rows_by_call=[_pending_row()])
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    set_params_calls: list = []
    monkeypatch.setattr(pm, '_set_params_via_manager',
                        lambda **kw: set_params_calls.append(kw) or {'after': {'eligible': False, 'size_scalar': 0.6}})
    result = pm.approve(proposal_id=42, actor='operator:t', reason='ok', source='dashboard')
    # Sequence: SELECT FOR UPDATE, then UPDATE proposals (after set_params)
    sql_starts = [e[0].strip().split()[0].upper() for e in conn.cur.executed]
    assert 'SELECT' in sql_starts
    assert 'UPDATE' in sql_starts
    # set_params should have been called with the proposed values
    assert len(set_params_calls) == 1
    assert set_params_calls[0]['strategy_id'] == 's1'
    assert set_params_calls[0]['regime_state'] == 'LOW_VOL'
    assert set_params_calls[0]['eligible'] is False
    assert set_params_calls[0]['size_scalar'] == 0.6
    assert result['status'] == 'approved'


def test_approve_rejects_already_decided(monkeypatch):
    """A proposal already approved/rejected can't be re-approved."""
    decided_row = (42, 's1', 'LOW_VOL', 'rejected',
                   False, None, None, None, None, 0.5, 'na', None)
    conn = FakeConn(rows_by_call=[decided_row])
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    with pytest.raises(ValueError, match='not pending'):
        pm.approve(proposal_id=42, actor='operator:t', reason='', source='cli')


def test_approve_rejects_missing_proposal(monkeypatch):
    conn = FakeConn(rows_by_call=[])
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    with pytest.raises(KeyError):
        pm.approve(proposal_id=999, actor='operator:t', reason='', source='cli')


def test_reject_does_not_call_set_params(monkeypatch):
    conn = FakeConn(rows_by_call=[_pending_row()])
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    set_params_calls: list = []
    monkeypatch.setattr(pm, '_set_params_via_manager',
                        lambda **kw: set_params_calls.append(kw) or {})
    result = pm.reject(proposal_id=42, actor='operator:t',
                       reason='too aggressive', source='dashboard')
    assert set_params_calls == []
    assert result['status'] == 'rejected'


def test_modify_overrides_proposed_values(monkeypatch):
    """Operator can override individual proposed columns at approve-time."""
    conn = FakeConn(rows_by_call=[_pending_row(proposed_size=0.6)])
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    set_params_calls: list = []
    monkeypatch.setattr(pm, '_set_params_via_manager',
                        lambda **kw: set_params_calls.append(kw) or {'after': {'eligible': False, 'size_scalar': 0.8}})
    result = pm.modify(proposal_id=42, actor='operator:t',
                       overrides={'size_scalar': 0.8},
                       reason='operator prefers gentler trim', source='dashboard')
    assert set_params_calls[0]['size_scalar'] == 0.8        # overridden
    assert set_params_calls[0]['eligible'] is False        # from proposal
    assert result['status'] == 'modified'


def test_list_proposals_filters_status(monkeypatch):
    rows = [
        (1, 's1', 'LOW_VOL', 'pending', True, 0.5, None, None, None,
         0.8, 'r', None, None, None, None, None),
    ]

    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=()): self.last = (sql, params)
        def fetchall(self): return rows
        @property
        def description(self):
            cols = ('id', 'strategy_id', 'regime_state', 'status',
                    'proposed_eligible', 'proposed_size_scalar',
                    'proposed_stop_pct', 'proposed_target_pct',
                    'proposed_max_hold_days', 'confidence', 'reasoning',
                    'memo_id', 'proposed_at', 'decided_at', 'decided_by',
                    'decision_reason')
            return [type('C', (), {'name': c}) for c in cols]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return C()
        def commit(self): pass

    monkeypatch.setattr(pm, '_connect', lambda: Conn())
    out = pm.list_proposals(status='pending', limit=10)
    assert len(out) == 1
    assert out[0]['status'] == 'pending'


def test_supersede_marks_old_pending_for_same_pair(monkeypatch):
    """When Mastermind emits a new proposal for an already-pending (strategy, regime),
    the old one auto-supersedes."""
    conn = FakeConn(rows_by_call=[])  # supersede is just an UPDATE, no fetch
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    pm.supersede_pending('s1', 'LOW_VOL', new_proposer='mastermind:run-2')
    sql_starts = [e[0].strip().split()[0].upper() for e in conn.cur.executed]
    assert sql_starts == ['UPDATE']
    sql = conn.cur.executed[0][0]
    assert 'superseded' in sql
    assert 'pending' in sql


def test_insert_proposal_returns_id(monkeypatch):
    conn = FakeConn(rows_by_call=[(123,)])  # RETURNING id
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    pid = pm.insert_proposal(
        proposer='mastermind:run-1',
        strategy_id='s1', regime_state='LOW_VOL',
        current_row={'eligible': True, 'size_scalar': None,
                     'stop_pct': None, 'target_pct': None, 'max_hold_days': None},
        proposed_eligible=False,
        proposed_size_scalar=0.6,
        proposed_stop_pct=None,
        proposed_target_pct=None,
        proposed_max_hold_days=None,
        confidence=0.75, reasoning='trim per live perf',
        memo_id=None,
    )
    assert pid == 123
    sql_starts = [e[0].strip().split()[0].upper() for e in conn.cur.executed]
    assert sql_starts == ['INSERT']
    assert 'RETURNING id' in conn.cur.executed[0][0]
