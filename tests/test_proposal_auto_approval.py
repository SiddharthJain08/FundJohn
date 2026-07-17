"""Tests for proposal_manager.auto_approve — bounded auto-approval."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import proposal_manager as pm  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows or [])
        self.executed: list = []
        self.rowcount = 0
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=()): self.executed.append((sql, params))
    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class FakeConn:
    def __init__(self, rows=()):
        self.cur = FakeCursor(rows)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def cursor(self): return self.cur
    def commit(self): pass


def _proposal_row(pid=10, conf=0.9, size=0.2, eligible=True):
    # Tuple shape matches _PENDING_COLS in proposal_manager
    return (pid, 's1', 'LOW_VOL', 'pending', eligible, size,
            None, None, None, conf, 'looks good', None)


def test_auto_approve_disabled_by_default(monkeypatch):
    """OPENCLAW_PROPOSAL_AUTOAPPROVE not set → no-op."""
    monkeypatch.delenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', raising=False)
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_proposal_row()]))
    monkeypatch.setattr(pm, '_set_params_via_manager',
                        lambda **kw: {'after': {}})
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert 'disabled' in result['reason'].lower()


def test_auto_approve_below_confidence_threshold(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.85')
    monkeypatch.setattr(pm, '_connect',
                        lambda: FakeConn(rows=[_proposal_row(conf=0.7)]))
    monkeypatch.setattr(pm, '_set_params_via_manager',
                        lambda **kw: {'after': {}})
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert 'confidence' in result['reason'].lower()


def test_auto_approve_size_delta_too_large(monkeypatch):
    """proposed_size 0.5 vs current size 0 → delta 0.5 > 0.20 limit."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.85')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_SIZE_DELTA', '0.20')
    monkeypatch.setattr(pm, '_connect',
                        lambda: FakeConn(rows=[_proposal_row(conf=0.95, size=0.5)]))
    # Stub the current-row lookup (returns None → current_size treated as 0)
    monkeypatch.setattr(pm, '_current_size_scalar', lambda sid, r: None)
    monkeypatch.setattr(pm, '_set_params_via_manager',
                        lambda **kw: {'after': {}})
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert 'size' in result['reason'].lower()


def test_auto_approve_happy_path(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.85')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_SIZE_DELTA', '0.20')
    monkeypatch.setattr(pm, '_connect',
                        lambda: FakeConn(rows=[_proposal_row(conf=0.95, size=0.55)]))
    monkeypatch.setattr(pm, '_current_size_scalar', lambda sid, r: 0.5)
    set_params_calls: list = []
    monkeypatch.setattr(pm, '_set_params_via_manager',
                        lambda **kw: set_params_calls.append(kw) or {'after': {'eligible': True, 'size_scalar': 0.55}})
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'approved'
    assert len(set_params_calls) == 1
    assert set_params_calls[0]['size_scalar'] == 0.55


def test_auto_approve_eligibility_only_passes(monkeypatch):
    """eligibility-only proposal with no numeric deltas always passes size-delta check."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.85')
    monkeypatch.setattr(pm, '_connect',
                        lambda: FakeConn(rows=[(10, 's1', 'LOW_VOL', 'pending',
                                                 True, None, None, None, None,
                                                 0.95, 'expand to LOW_VOL', None)]))
    monkeypatch.setattr(pm, '_current_size_scalar', lambda sid, r: None)
    monkeypatch.setattr(pm, '_set_params_via_manager',
                        lambda **kw: {'after': {'eligible': True}})
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'approved'


# ── auto_apply_batch (2026-07-14 Saturday full-auto) ────────────────────────

def _batch_props(*specs):
    """specs: (pid, confidence) tuples → minimal list_proposals row dicts."""
    return [{'id': pid, 'strategy_id': f's{pid}', 'regime_state': 'LOW_VOL',
             'confidence': conf} for pid, conf in specs]


def test_auto_apply_batch_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', raising=False)
    monkeypatch.setattr(pm, 'list_proposals',
                        lambda **kw: (_ for _ in ()).throw(AssertionError('must not query')))
    out = pm.auto_apply_batch(log=lambda *_: None)
    assert out == {'skipped': True, 'approved': 0, 'noted': 0, 'errors': 0}


def test_auto_apply_batch_strict_threshold_split(monkeypatch):
    """conf 0.9 → approved; conf 0.8 (== threshold, strict >) → noted;
    conf None → noted; rail-skip inside auto_approve → noted."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.8')
    monkeypatch.setattr(pm, 'list_proposals', lambda **kw: _batch_props(
        (1, 0.9), (2, 0.8), (3, None), (4, 0.95)))
    approved_ids, noted = [], {}
    def _fake_auto(proposal_id):
        if proposal_id == 4:                       # rail skip
            return {'id': proposal_id, 'status': 'skipped', 'reason': 'size delta too big'}
        approved_ids.append(proposal_id)
        return {'id': proposal_id, 'status': 'approved'}
    monkeypatch.setattr(pm, 'auto_approve', _fake_auto)
    monkeypatch.setattr(pm, '_mark_noted', lambda pid, reason: noted.__setitem__(pid, reason))
    out = pm.auto_apply_batch(log=lambda *_: None)
    assert approved_ids == [1]
    assert set(noted) == {2, 3, 4}
    assert 'rail skip' in noted[4]
    assert out['approved'] == 1 and out['noted'] == 3 and out['errors'] == 0
    assert out['threshold'] == 0.8


def test_auto_apply_batch_counts_errors(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setattr(pm, 'list_proposals', lambda **kw: _batch_props((7, 0.99)))
    def _boom(proposal_id):
        raise RuntimeError('db down')
    monkeypatch.setattr(pm, 'auto_approve', _boom)
    out = pm.auto_apply_batch(threshold=0.8, log=lambda *_: None)
    assert out['errors'] == 1 and out['approved'] == 0


def test_supersede_covers_noted_rows(monkeypatch):
    """supersede_pending must sweep BOTH 'pending' and 'noted' — the noted
    re-evaluation loop depends on it."""
    conn = FakeConn()
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    pm.supersede_pending('s1', 'LOW_VOL', 'mastermind:review-2026-07-14')
    sql = conn.cur.executed[0][0]
    assert "IN ('pending', 'noted')" in sql


def test_lock_for_decision_accepts_noted():
    cur = FakeCursor(rows=[(10, 's1', 'LOW_VOL', 'noted', True, 0.2,
                            None, None, None, 0.5, 'meh', None)])
    prop = pm._lock_for_decision(cur, 10)
    assert prop['status'] == 'noted'
