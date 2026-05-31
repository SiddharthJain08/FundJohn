"""
SP-6 Phase A — Task 12: doctor preflight + system_checks tests.

Covers:
  1. doctor.check_eod_mutual_exclusion  — both gates on → FAIL; else → PASS
  2. doctor.check_eod_gate_consistency  — partial activation → FAIL; all-on / all-off → PASS
  3. system_checks pipeline._eod_compute_health_fresh — gate guard + DB result cases
  4. system_checks pipeline._carried_set_present     — gate guard + count cases
  5. system_checks pipeline._gate_ran_today          — gate guard + sentinel cases

All DB calls are mocked (no live Postgres required).
Run:
    source ./worktree-env.sh && python3 -m pytest tests/test_sp6_doctor_checks.py -v
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor  # noqa: E402
from system_checks.checks import pipeline  # noqa: E402  (registers checks as side-effect)
from system_checks.types import Status  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _pg_mock(fetchone_return):
    """Build a _pg() context-manager mock that yields a conn whose cursor
    returns `fetchone_return` on `fetchone()`.

    `_pg()` is used as:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute(...)
            row = cur.fetchone()

    So we need:
        _pg().__enter__() -> conn
        conn.cursor().__enter__() -> cur
        cur.fetchone() -> fetchone_return
    """
    cur_mock = MagicMock()
    cur_mock.fetchone.return_value = fetchone_return

    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cur_mock)
    cursor_cm.__exit__ = MagicMock(return_value=False)

    conn_mock = MagicMock()
    conn_mock.cursor.return_value = cursor_cm

    conn_cm = MagicMock()
    conn_cm.__enter__ = MagicMock(return_value=conn_mock)
    conn_cm.__exit__ = MagicMock(return_value=False)

    pg = MagicMock(return_value=conn_cm)
    return pg


# ═══════════════════════════════════════════════════════════════════════════
# 1.  doctor.check_eod_mutual_exclusion
# ═══════════════════════════════════════════════════════════════════════════

class TestDoctorEodMutualExclusion:
    """Both EOD_SIGNAL_REGISTER=1 AND CLOSE_EXEC_LIVE=1 → FAIL; any other combo → PASS."""

    def test_both_on_fails(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
        monkeypatch.setenv('OPENCLAW_CLOSE_EXEC_LIVE', '1')
        res = doctor.check_eod_mutual_exclusion()
        assert res['severity'] == doctor.FAIL
        assert 'mutually exclusive' in res['detail']

    def test_only_eod_register_on_passes(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
        monkeypatch.delenv('OPENCLAW_CLOSE_EXEC_LIVE', raising=False)
        res = doctor.check_eod_mutual_exclusion()
        assert res['severity'] == doctor.PASS
        assert 'eod_signal_register' in res['detail']

    def test_only_close_exec_on_passes(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
        monkeypatch.setenv('OPENCLAW_CLOSE_EXEC_LIVE', '1')
        res = doctor.check_eod_mutual_exclusion()
        assert res['severity'] == doctor.PASS
        assert 'close_exec_live' in res['detail']

    def test_both_off_passes(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
        monkeypatch.delenv('OPENCLAW_CLOSE_EXEC_LIVE', raising=False)
        res = doctor.check_eod_mutual_exclusion()
        assert res['severity'] == doctor.PASS
        assert 'neither' in res['detail']

    def test_both_explicitly_zero_passes(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '0')
        monkeypatch.setenv('OPENCLAW_CLOSE_EXEC_LIVE', '0')
        res = doctor.check_eod_mutual_exclusion()
        assert res['severity'] == doctor.PASS


# ═══════════════════════════════════════════════════════════════════════════
# 2.  doctor.check_eod_gate_consistency
# ═══════════════════════════════════════════════════════════════════════════

_EOD_GATES = (
    'OPENCLAW_EOD_SIGNAL_REGISTER',
    'OPENCLAW_EOD_PREMARKET_GATE',
    'OPENCLAW_EOD_RECONCILE',
)


def _set_eod_gates(monkeypatch, *, register, premarket, reconcile):
    """Helper: set each gate to '1' or remove it (simulating unset)."""
    for env, val in zip(_EOD_GATES, (register, premarket, reconcile)):
        if val:
            monkeypatch.setenv(env, '1')
        else:
            monkeypatch.delenv(env, raising=False)


class TestDoctorEodGateConsistency:
    """Partial EOD-gate activation is the footgun: FAIL unless all-on or all-off."""

    def test_all_three_on_passes(self, monkeypatch):
        _set_eod_gates(monkeypatch, register=True, premarket=True, reconcile=True)
        res = doctor.check_eod_gate_consistency()
        assert res['severity'] == doctor.PASS
        assert 'all_on' in res['detail']

    def test_all_three_unset_passes(self, monkeypatch):
        _set_eod_gates(monkeypatch, register=False, premarket=False, reconcile=False)
        res = doctor.check_eod_gate_consistency()
        assert res['severity'] == doctor.PASS
        assert 'all_off' in res['detail']

    def test_register_on_others_off_fails(self, monkeypatch):
        """SIGNAL_REGISTER=1 without RECONCILE means sizer runs without APPROVED-set loading."""
        _set_eod_gates(monkeypatch, register=True, premarket=False, reconcile=False)
        res = doctor.check_eod_gate_consistency()
        assert res['severity'] == doctor.FAIL
        assert 'partial' in res['detail']

    def test_premarket_on_others_off_fails(self, monkeypatch):
        _set_eod_gates(monkeypatch, register=False, premarket=True, reconcile=False)
        res = doctor.check_eod_gate_consistency()
        assert res['severity'] == doctor.FAIL

    def test_reconcile_on_others_off_fails(self, monkeypatch):
        _set_eod_gates(monkeypatch, register=False, premarket=False, reconcile=True)
        res = doctor.check_eod_gate_consistency()
        assert res['severity'] == doctor.FAIL

    def test_two_on_one_off_fails(self, monkeypatch):
        """Two of three on still partial — reconcile missing is the worst case."""
        _set_eod_gates(monkeypatch, register=True, premarket=True, reconcile=False)
        res = doctor.check_eod_gate_consistency()
        assert res['severity'] == doctor.FAIL
        assert 'EOD_RECONCILE' in res['detail']

    def test_register_on_reconcile_on_premarket_off_fails(self, monkeypatch):
        """Signal register + reconcile on but premarket gate off is also partial."""
        _set_eod_gates(monkeypatch, register=True, premarket=False, reconcile=True)
        res = doctor.check_eod_gate_consistency()
        assert res['severity'] == doctor.FAIL

    def test_detail_includes_off_gate_name(self, monkeypatch):
        """Detail must name the off gate so operator knows what to flip."""
        _set_eod_gates(monkeypatch, register=True, premarket=True, reconcile=False)
        res = doctor.check_eod_gate_consistency()
        assert 'EOD_RECONCILE' in res['detail']

    def test_all_checks_discoverable(self):
        """Both new doctor checks appear in _all_checks() discovery."""
        names = {n for n, _, _ in doctor._all_checks()}
        assert 'eod_mutual_exclusion' in names
        assert 'eod_gate_consistency' in names


# ═══════════════════════════════════════════════════════════════════════════
# 3.  system_checks pipeline._eod_compute_health_fresh
# ═══════════════════════════════════════════════════════════════════════════

class TestEodComputeHealthFresh:
    """Gate-conditional freshness check for eod_compute_health table."""

    def test_gate_off_skips(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
        result = pipeline._eod_compute_health_fresh()
        assert result[0] is Status.SKIP
        assert 'OFF' in result[1]

    def test_gate_off_explicit_zero_skips(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '0')
        result = pipeline._eod_compute_health_fresh()
        assert result[0] is Status.SKIP

    def test_no_row_warns(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
        pg = _pg_mock(fetchone_return=None)
        with patch('system_checks.checks.pipeline._pg', pg):
            result = pipeline._eod_compute_health_fresh()
        assert result[0] is Status.WARN
        assert 'no eod_compute_health row' in result[1]

    def test_today_healthy_passes(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
        today = date.today()
        pg = _pg_mock(fetchone_return=(True, today))
        with patch('system_checks.checks.pipeline._pg', pg):
            result = pipeline._eod_compute_health_fresh()
        assert result[0] is Status.PASS
        assert 'fresh and healthy' in result[1]

    def test_stale_row_warns(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
        yesterday = date.today() - timedelta(days=1)
        pg = _pg_mock(fetchone_return=(True, yesterday))
        with patch('system_checks.checks.pipeline._pg', pg):
            result = pipeline._eod_compute_health_fresh()
        assert result[0] is Status.WARN
        assert '1d old' in result[1]

    def test_unhealthy_warns(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
        today = date.today()
        pg = _pg_mock(fetchone_return=(False, today))
        with patch('system_checks.checks.pipeline._pg', pg):
            result = pipeline._eod_compute_health_fresh()
        assert result[0] is Status.WARN
        assert 'unhealthy' in result[1]


# ═══════════════════════════════════════════════════════════════════════════
# 4.  system_checks pipeline._carried_set_present
# ═══════════════════════════════════════════════════════════════════════════

class TestCarriedSetPresent:
    """Gate-conditional check: COMPUTED/APPROVED signals must exist for today."""

    def test_gate_off_skips(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
        result = pipeline._carried_set_present()
        assert result[0] is Status.SKIP

    def test_gate_on_zero_rows_warns(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
        pg = _pg_mock(fetchone_return=(0,))
        with patch('system_checks.checks.pipeline._pg', pg):
            result = pipeline._carried_set_present()
        assert result[0] is Status.WARN
        assert '0 COMPUTED/APPROVED' in result[1]

    def test_gate_on_rows_present_passes(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
        pg = _pg_mock(fetchone_return=(7,))
        with patch('system_checks.checks.pipeline._pg', pg):
            result = pipeline._carried_set_present()
        assert result[0] is Status.PASS
        assert '7' in result[1]


# ═══════════════════════════════════════════════════════════════════════════
# 5.  system_checks pipeline._gate_ran_today
# ═══════════════════════════════════════════════════════════════════════════

class TestGateRanToday:
    """Gate-conditional check: __gate_ran__ sentinel must exist for today."""

    def test_gate_off_skips(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_EOD_PREMARKET_GATE', raising=False)
        result = pipeline._gate_ran_today()
        assert result[0] is Status.SKIP

    def test_gate_on_no_sentinel_warns(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_PREMARKET_GATE', '1')
        pg = _pg_mock(fetchone_return=(0,))
        with patch('system_checks.checks.pipeline._pg', pg):
            result = pipeline._gate_ran_today()
        assert result[0] is Status.WARN
        assert '__gate_ran__' in result[1] or 'sentinel' in result[1]

    def test_gate_on_sentinel_present_passes(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EOD_PREMARKET_GATE', '1')
        pg = _pg_mock(fetchone_return=(1,))
        with patch('system_checks.checks.pipeline._pg', pg):
            result = pipeline._gate_ran_today()
        assert result[0] is Status.PASS

    def test_gate_ran_query_includes_sentinel_filter(self, monkeypatch):
        """Verify the SQL uses gate_type='__gate_ran__' (not a count of all verdicts).
        We check the SQL string captured by the cursor mock."""
        monkeypatch.setenv('OPENCLAW_EOD_PREMARKET_GATE', '1')

        cur_mock = MagicMock()
        cur_mock.fetchone.return_value = (1,)
        cursor_cm = MagicMock()
        cursor_cm.__enter__ = MagicMock(return_value=cur_mock)
        cursor_cm.__exit__ = MagicMock(return_value=False)
        conn_mock = MagicMock()
        conn_mock.cursor.return_value = cursor_cm
        conn_cm = MagicMock()
        conn_cm.__enter__ = MagicMock(return_value=conn_mock)
        conn_cm.__exit__ = MagicMock(return_value=False)
        pg = MagicMock(return_value=conn_cm)

        with patch('system_checks.checks.pipeline._pg', pg):
            pipeline._gate_ran_today()

        sql_called = cur_mock.execute.call_args[0][0]
        assert '__gate_ran__' in sql_called, (
            "SQL must filter gate_type='__gate_ran__' not count all verdicts"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 6.  system_checks registration smoke (checks appear in registry)
# ═══════════════════════════════════════════════════════════════════════════

class TestSystemChecksRegistered:
    """The three new checks must appear in the system_checks registry."""

    def test_eod_compute_health_registered(self):
        from system_checks.registry import all_checks
        assert 'eod_compute_health_fresh' in all_checks()

    def test_carried_set_present_registered(self):
        from system_checks.registry import all_checks
        assert 'carried_set_present' in all_checks()

    def test_gate_ran_today_registered(self):
        from system_checks.registry import all_checks
        assert 'gate_ran_today' in all_checks()

    def test_all_three_tagged_pipeline(self):
        from system_checks.registry import all_checks
        reg = all_checks()
        for name in ('eod_compute_health_fresh', 'carried_set_present', 'gate_ran_today'):
            assert 'pipeline' in reg[name]['tags'], f'{name} missing pipeline tag'
