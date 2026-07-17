"""Tests for eligibility_manager after DB switch. The old test file
(test_eligibility_manager.py) tested the manifest-write path; it is
being replaced by these DB-write tests."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import eligibility_manager as em  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.executed: list = []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=()): self.executed.append((sql, params))
    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class FakeConn:
    def __init__(self, rows=()): self.cur = FakeCursor(rows)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def cursor(self): return self.cur
    def commit(self): self.committed = True


def test_set_params_writes_audit_then_params(monkeypatch):
    """Single transaction: SELECT FOR UPDATE → INSERT audit → upsert."""
    conn = FakeConn(rows=[('s1', 'LOW_VOL', True, None, None, None, None)])
    monkeypatch.setattr(em, '_connect', lambda: conn)
    invalidations: list = []
    monkeypatch.setattr(em, '_invalidate_cache',
                        lambda sid, r: invalidations.append((sid, r)))
    em.set_params(strategy_id='s1', regime_state='LOW_VOL',
                  eligible=False, actor='operator:t', reason='', source='cli')
    sql_starts = [e[0].strip().split()[0].upper() for e in conn.cur.executed]
    assert sql_starts == ['SELECT', 'INSERT', 'INSERT']
    assert 'strategy_regime_param_changes' in conn.cur.executed[1][0]
    assert 'strategy_regime_params' in conn.cur.executed[2][0]
    assert 'ON CONFLICT' in conn.cur.executed[2][0]
    assert invalidations == [('s1', 'LOW_VOL')]


def test_set_params_rejects_unknown_regime():
    with pytest.raises(ValueError, match='invalid regime'):
        em.set_params(strategy_id='s1', regime_state='BOGUS',
                      eligible=True, actor='t', reason='', source='cli')


def test_set_params_requires_at_least_one_field(monkeypatch):
    """At least one of eligible/size_scalar/stop_pct/target_pct/max_hold_days
    must be specified, otherwise the call is a no-op (rejected)."""
    monkeypatch.setattr(em, '_connect', lambda: FakeConn())
    with pytest.raises(ValueError, match='at least one'):
        em.set_params(strategy_id='s1', regime_state='LOW_VOL',
                      actor='t', reason='', source='cli')


def test_set_params_merges_partial_update(monkeypatch):
    """NULL caller arg means 'keep existing'; non-None overrides."""
    existing = ('s1', 'LOW_VOL', True, 0.5, None, None, None)
    conn = FakeConn(rows=[existing])
    monkeypatch.setattr(em, '_connect', lambda: conn)
    monkeypatch.setattr(em, '_invalidate_cache', lambda sid, r: None)
    em.set_params(strategy_id='s1', regime_state='LOW_VOL',
                  size_scalar=0.7,   # caller wants to change just this
                  actor='t', reason='', source='cli')
    upsert_call = conn.cur.executed[2]
    # 8 params: strategy_id, regime, eligible, size, stop, target, max_hold, set_by
    assert upsert_call[1][2] is True       # eligible preserved
    assert float(upsert_call[1][3]) == 0.7  # size_scalar updated
    assert upsert_call[1][4] is None       # stop_pct preserved (was None)
