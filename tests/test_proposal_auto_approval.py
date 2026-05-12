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
