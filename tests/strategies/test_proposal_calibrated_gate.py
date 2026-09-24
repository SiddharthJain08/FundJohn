"""D2b — auto_approve compares min(calibrated, cap) under the flag.

Every DB touchpoint is a fake cursor; the calibration report and the evidence
query are monkeypatched. No psycopg2 connection is opened.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    python3 -m pytest tests/strategies/test_proposal_calibrated_gate.py -q
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
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
        self.committed = 0
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def cursor(self): return self.cur
    def commit(self): self.committed += 1


def _row(pid=10, conf=0.9, size=None):
    # Tuple shape matches _PENDING_COLS
    return (pid, 's1', 'LOW_VOL', 'pending', True, size,
            None, None, None, conf, 'looks good', None)


def _buckets(n=18, rate=0.56):
    rows = []
    from metrics import mastermind_calibration as mc
    for lo, hi, label in mc.BUCKETS:
        if label == '[0.8, 1.0]':
            rows.append({'range': label, 'count': n, 'matched': int(n * rate), 'match_rate': rate})
        else:
            rows.append({'range': label, 'count': 0, 'matched': 0, 'match_rate': None})
    return rows


@pytest.fixture
def wired(monkeypatch):
    """Common wiring: env on, floor 0.9, one pending proposal, stubbed calibration."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.9')
    monkeypatch.delenv('OPENCLAW_PROPOSAL_CALIBRATED', raising=False)
    conns = []
    def _connect():
        c = FakeConn(rows=[_row()])
        conns.append(c)
        return c
    monkeypatch.setattr(pm, '_connect', _connect)
    monkeypatch.setattr(pm, '_calibration_report', lambda: {'buckets': _buckets()})
    monkeypatch.setattr(pm, '_evidence_counts',
                        lambda sid, regime: {'n_closed': 12, 'staleness_days': 5.0})
    recorded = {}
    monkeypatch.setattr(pm, '_record_calibration',
                        lambda pid, calib: recorded.update({'pid': pid, **calib}))
    decided = {}
    monkeypatch.setattr(pm, '_decide',
                        lambda **kw: decided.update(kw) or {'id': kw['proposal_id'],
                                                            'status': 'approved'})
    return recorded, decided


def test_shadow_mode_records_but_does_not_change_the_decision(wired, monkeypatch):
    recorded, decided = wired
    monkeypatch.delenv('OPENCLAW_PROPOSAL_CALIBRATED', raising=False)
    result = pm.auto_approve(proposal_id=10)
    # raw 0.9 >= floor 0.9 -> still approved, exactly as today
    assert result['status'] == 'approved'
    assert decided['terminal_status'] == 'approved'
    # ...but the shadow numbers were computed and recorded
    assert recorded['raw'] == pytest.approx(0.9)
    assert recorded['calibrated'] == pytest.approx(0.56, abs=1e-9)
    assert recorded['cap'] == pytest.approx(0.55)
    assert recorded['binding_bound'] == 'cap'
    assert result['calibration']['enforced'] is False
    assert result['calibration']['would_skip'] is True


def test_shadow_mode_logs_the_would_be_decision(wired, monkeypatch, caplog):
    """Spec D2: 'env-unset path logs and does not change the decision.'"""
    monkeypatch.delenv('OPENCLAW_PROPOSAL_CALIBRATED', raising=False)
    with caplog.at_level(logging.INFO, logger='strategies.proposal_manager'):
        result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'approved'
    assert 'calibration SHADOW' in caplog.text
    assert 'would_skip=True' in caplog.text


def test_unset_path_auto_reason_is_byte_identical_to_base(wired, monkeypatch):
    """§0 / spec D2 IMPORTANT #2: with OPENCLAW_PROPOSAL_CALIBRATED unset,
    auto_reason (== decision_reason == the set_params audit text) must be
    byte-identical to base's format -- no calibrated=/cap= fragments. The
    calibration facts live only in the five recorded columns + the SHADOW
    log line, never in the audit string itself."""
    recorded, decided = wired
    monkeypatch.delenv('OPENCLAW_PROPOSAL_CALIBRATED', raising=False)
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'approved'
    # Pinned literal, not rebuilt from the code under test (`wired` fixes the
    # floor at 0.9 and leaves size/stop deltas at their 0.20/0.01 defaults) --
    # this must match base's pre-D2b format byte-for-byte.
    expected = 'auto-approved: confidence=0.90 >= 0.9, rails (size_delta<=0.2, stop<=0.01) all passed'
    assert decided['reason'] == expected
    assert 'calibrated=' not in decided['reason']
    assert 'cap=' not in decided['reason']


def test_enforced_mode_skips_when_the_min_is_below_the_floor(wired, monkeypatch):
    recorded, decided = wired
    monkeypatch.setenv('OPENCLAW_PROPOSAL_CALIBRATED', '1')
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert 'calibrated' in result['reason']
    assert decided == {}, '_decide must not be called when the calibrated bound fails'
    assert recorded['binding_bound'] == 'cap'
    # evidence succeeded here (the `wired` fixture stubs it cleanly) -> no
    # evidence_error marker in the reason.
    assert 'evidence_error' not in result['reason']


def test_enforced_mode_approves_when_both_bounds_clear_the_floor(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.5')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_CALIBRATED', '1')
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_row(conf=0.9)]))
    monkeypatch.setattr(pm, '_calibration_report', lambda: {'buckets': _buckets(n=20, rate=0.9)})
    monkeypatch.setattr(pm, '_evidence_counts',
                        lambda sid, regime: {'n_closed': 250, 'staleness_days': 1.0})
    monkeypatch.setattr(pm, '_record_calibration', lambda pid, calib: None)
    decided = {}
    monkeypatch.setattr(pm, '_decide',
                        lambda **kw: decided.update(kw) or {'id': 10, 'status': 'approved'})
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'approved'
    assert decided['terminal_status'] == 'approved'
    assert result['calibration']['calibrated'] == pytest.approx(0.9)
    assert result['calibration']['cap'] == 1.0
    assert result['calibration']['binding_bound'] == 'calibrated'


def test_calibration_failure_falls_back_to_raw_and_never_raises(monkeypatch):
    """Both the report and evidence steps raise. The two failure domains are
    independent: calibration fails OPEN (calibrated -> raw), but evidence
    fails CLOSED (cap -> 0.35/'none'), so the enforced compare -- which
    takes min(calibrated, cap) -- ends up SKIPPED at 0.35, not approved at
    a permissive cap==1.0. No exception escapes either way."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.9')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_CALIBRATED', '1')
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_row(conf=0.95)]))
    def _boom(*a, **k): raise RuntimeError('calibration table missing')
    monkeypatch.setattr(pm, '_calibration_report', _boom)
    monkeypatch.setattr(pm, '_evidence_counts', _boom)
    monkeypatch.setattr(pm, '_record_calibration', lambda pid, calib: None)
    def _must_not_decide(**kw):
        raise AssertionError('_decide must not be called when the evidence cap bites')
    monkeypatch.setattr(pm, '_decide', _must_not_decide)
    result = pm.auto_approve(proposal_id=10)
    # fail-open (calibration) + fail-closed (evidence) -> effective = min(0.95, 0.35)
    # = 0.35 < floor 0.9 -> skipped. No exception escapes.
    assert result['status'] == 'skipped'
    assert result['calibration']['error'] is not None
    assert result['calibration']['evidence_error'] is True
    assert result['calibration']['calibrated'] == pytest.approx(0.95)
    assert result['calibration']['cap'] == pytest.approx(0.35)
    assert result['calibration']['evidence_level'] == 'none'
    assert 'cap 0.35 [none]' in result['reason']
    assert 'evidence_error' in result['reason']


def test_nan_raw_on_the_fail_open_path_never_reaches_min_of_nan(monkeypatch):
    """Minor #3: a NaN raw confidence on the fail-open (report-failure) path
    must resolve `calibrated` to None -- never a bare NaN -- so `effective`
    is None and the proposal skips, rather than an undefined min(nan, cap)
    compare silently doing something wrong."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_CALIBRATED', '1')
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_row(conf=float('nan'))]))
    def _boom(): raise RuntimeError('calibration report table missing')
    monkeypatch.setattr(pm, '_calibration_report', _boom)
    monkeypatch.setattr(pm, '_evidence_counts',
                        lambda sid, regime: {'n_closed': 250, 'staleness_days': 1.0})
    monkeypatch.setattr(pm, '_record_calibration', lambda pid, calib: None)
    def _must_not_decide(**kw):
        raise AssertionError('_decide must not be called for a NaN-confidence proposal')
    monkeypatch.setattr(pm, '_decide', _must_not_decide)
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert result['calibration']['calibrated'] is None
    assert result['calibration']['effective'] is None


def test_report_failure_alone_falls_back_to_raw_but_evidence_cap_is_real(monkeypatch):
    """Report raises; evidence succeeds normally -> cap reflects the REAL
    evidence-derived tier (not the fail-closed default) -- only the
    calibration step failed open. The two failure domains are independent."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.5')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_CALIBRATED', '1')
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_row(conf=0.9)]))
    def _boom(): raise RuntimeError('calibration report table missing')
    monkeypatch.setattr(pm, '_calibration_report', _boom)
    monkeypatch.setattr(pm, '_evidence_counts',
                        lambda sid, regime: {'n_closed': 50, 'staleness_days': 1.0})
    monkeypatch.setattr(pm, '_record_calibration', lambda pid, calib: None)
    monkeypatch.setattr(pm, '_decide', lambda **kw: {'id': 10, 'status': 'approved'})
    result = pm.auto_approve(proposal_id=10)
    assert result['calibration']['error'] is not None
    assert result['calibration']['evidence_error'] is False
    assert result['calibration']['calibrated'] == pytest.approx(0.9)   # fail-open to raw
    assert result['calibration']['evidence_level'] == 'medium'         # real evidence tier
    assert result['calibration']['cap'] == pytest.approx(0.75)
    assert result['status'] == 'approved'   # min(0.9, 0.75) = 0.75 >= floor 0.5


def test_evidence_failure_alone_fails_closed_even_though_calibrated_would_pass(monkeypatch):
    """Evidence raises; report succeeds normally -> cap forced to 'none'/0.35
    regardless of how healthy the calibrated number is. Fails CLOSED, not
    open: the calibrated bound alone (0.56) would clear a 0.5 floor, but the
    evidence-failure cap (0.35) still bites."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.5')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_CALIBRATED', '1')
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_row(conf=0.9)]))
    monkeypatch.setattr(pm, '_calibration_report', lambda: {'buckets': _buckets()})
    def _boom(sid, regime): raise RuntimeError('signal_pnl join timed out')
    monkeypatch.setattr(pm, '_evidence_counts', _boom)
    monkeypatch.setattr(pm, '_record_calibration', lambda pid, calib: None)
    def _must_not_decide(**kw):
        raise AssertionError('_decide must not be called when the evidence cap bites')
    monkeypatch.setattr(pm, '_decide', _must_not_decide)
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert result['calibration']['error'] is not None
    assert result['calibration']['evidence_error'] is True
    assert result['calibration']['evidence_level'] == 'none'
    assert result['calibration']['cap'] == pytest.approx(0.35)
    assert result['calibration']['calibrated'] == pytest.approx(0.56, abs=1e-9)  # report OK
    assert 'cap 0.35 [none]' in result['reason']
    assert 'evidence_error' in result['reason']


def test_recording_happens_before_the_size_rail(monkeypatch):
    """A proposal that dies on the size rail must still leave shadow evidence."""
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', '1')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE', '0.5')
    monkeypatch.setenv('OPENCLAW_PROPOSAL_AUTOAPPROVE_MAX_SIZE_DELTA', '0.20')
    monkeypatch.delenv('OPENCLAW_PROPOSAL_CALIBRATED', raising=False)
    monkeypatch.setattr(pm, '_connect', lambda: FakeConn(rows=[_row(conf=0.9, size=0.9)]))
    monkeypatch.setattr(pm, '_calibration_report', lambda: {'buckets': _buckets()})
    monkeypatch.setattr(pm, '_evidence_counts',
                        lambda sid, regime: {'n_closed': 12, 'staleness_days': 5.0})
    monkeypatch.setattr(pm, '_current_size_scalar', lambda sid, r: 0.0)
    recorded = {}
    monkeypatch.setattr(pm, '_record_calibration',
                        lambda pid, calib: recorded.update(calib))
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert 'size' in result['reason'].lower()
    assert recorded['calibrated'] == pytest.approx(0.56, abs=1e-9)


def test_disabled_feature_short_circuits_before_any_calibration(monkeypatch):
    monkeypatch.delenv('OPENCLAW_PROPOSAL_AUTOAPPROVE', raising=False)
    def _boom(*a, **k): raise AssertionError('must not be called')
    monkeypatch.setattr(pm, '_calibration_report', _boom)
    monkeypatch.setattr(pm, '_connect', _boom)
    result = pm.auto_approve(proposal_id=10)
    assert result['status'] == 'skipped'
    assert 'disabled' in result['reason'].lower()


def test_record_calibration_writes_the_five_columns_in_order(monkeypatch):
    """Pins the SET column list and the bound parameter order/values that
    migration 159's columns must line up with."""
    conn = FakeConn()
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    calib = {'raw': 0.9, 'calibrated': 0.56, 'cap': 0.55,
             'evidence_level': 'low', 'binding_bound': 'cap'}
    pm._record_calibration(10, calib)
    assert conn.committed == 1
    sql, params = conn.cur.executed[0]
    sql_lower = sql.lower()
    for col in ('confidence_raw', 'confidence_calibrated', 'evidence_cap',
                'evidence_level', 'binding_bound'):
        assert col in sql_lower
    assert params == (0.9, 0.56, 0.55, 'low', 'cap', 10)


def test_record_calibration_never_raises_on_a_connect_failure(monkeypatch, caplog):
    """Best-effort: a recording failure must never block a decision."""
    def _boom():
        raise RuntimeError('db unreachable')
    monkeypatch.setattr(pm, '_connect', _boom)
    with caplog.at_level(logging.WARNING, logger='strategies.proposal_manager'):
        pm._record_calibration(10, {'raw': 0.9, 'calibrated': 0.56, 'cap': 0.55,
                                     'evidence_level': 'low', 'binding_bound': 'cap'})
    assert 'calibration recording failed' in caplog.text


def test_migration_159_adds_only_columns():
    sql = (ROOT / 'src' / 'database' / 'migrations'
           / '159_proposal_calibration.sql').read_text().lower()
    for col in ('confidence_raw', 'confidence_calibrated', 'evidence_cap',
                'evidence_level', 'binding_bound'):
        assert f'add column if not exists {col}' in sql
    for forbidden in ('drop column', 'drop table', 'delete from', 'truncate'):
        assert forbidden not in sql
