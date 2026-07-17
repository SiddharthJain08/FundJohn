"""Fix 3 — the 'pipeline ran today' checks must SKIP on non-trading days.

On NYSE holidays (e.g. Juneteenth 2026-06-19) and weekends no pipeline runs,
so pipeline_completed_today / signals_persisted_today / handoff_written_today /
carried_set_present WARN/FAIL spuriously. They must SKIP first.

_is_trading_day(d) asks the broker calendar (`alpaca calendar`): a non-empty
JSON array => trading day; [] => holiday/weekend. On any CLI error it falls
back to a weekday check (Mon-Fri => trading day).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from system_checks.checks import pipeline  # noqa: E402
from system_checks.types import Status  # noqa: E402


def _proc(returncode=0, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ── _is_trading_day unit tests (CLI mocked) ──────────────────────────────

class TestIsTradingDay:
    def test_nonempty_calendar_is_trading_day(self):
        with patch('system_checks.checks.pipeline.subprocess.run',
                   return_value=_proc(0, '[{"date":"2026-06-18","open":"09:30"}]', '')):
            assert pipeline._is_trading_day(date(2026, 6, 18)) is True

    def test_empty_calendar_is_holiday(self):
        with patch('system_checks.checks.pipeline.subprocess.run',
                   return_value=_proc(0, '[]', '')):
            assert pipeline._is_trading_day(date(2026, 6, 19)) is False

    def test_cli_error_falls_back_to_weekday_true(self):
        """CLI failure on a weekday -> assume trading day (checks still fire)."""
        with patch('system_checks.checks.pipeline.subprocess.run',
                   side_effect=FileNotFoundError('no alpaca')):
            # 2026-06-18 is a Thursday
            assert pipeline._is_trading_day(date(2026, 6, 18)) is True

    def test_cli_error_falls_back_to_weekend_false(self):
        """CLI failure on a Saturday -> not a trading day."""
        with patch('system_checks.checks.pipeline.subprocess.run',
                   side_effect=FileNotFoundError('no alpaca')):
            # 2026-06-20 is a Saturday
            assert pipeline._is_trading_day(date(2026, 6, 20)) is False

    def test_cli_timeout_falls_back(self):
        import subprocess as _sp
        with patch('system_checks.checks.pipeline.subprocess.run',
                   side_effect=_sp.TimeoutExpired('alpaca', 10)):
            assert pipeline._is_trading_day(date(2026, 6, 18)) is True  # Thu
            assert pipeline._is_trading_day(date(2026, 6, 20)) is False  # Sat

    def test_cli_nonzero_rc_falls_back(self):
        with patch('system_checks.checks.pipeline.subprocess.run',
                   return_value=_proc(1, '', 'boom')):
            assert pipeline._is_trading_day(date(2026, 6, 18)) is True  # Thu

    def test_cli_bad_json_falls_back(self):
        with patch('system_checks.checks.pipeline.subprocess.run',
                   return_value=_proc(0, 'not json', '')):
            assert pipeline._is_trading_day(date(2026, 6, 18)) is True  # Thu


# ── The 4 guarded checks SKIP on non-trading days ────────────────────────

GUARDED = [
    'pipeline_completed_today',
    'signals_persisted_today',
    'handoff_written_today',
    'carried_set_present',
]


class TestGuardedChecksSkip:
    def test_all_four_skip_on_non_trading_day(self):
        with patch('system_checks.checks.pipeline._is_trading_day', return_value=False):
            for name in GUARDED:
                fn = getattr(pipeline, '_' + name)
                status, msg = fn()
                assert status is Status.SKIP, f'{name} should SKIP, got {status}'
                assert 'not a trading day' in msg, f'{name} msg: {msg}'

    def test_pipeline_completed_runs_normally_on_trading_day(self):
        """With _is_trading_day True and no log present, the existing logic
        runs and returns WARN (proves the guard didn't short-circuit)."""
        with patch('system_checks.checks.pipeline._is_trading_day', return_value=True), \
             patch.object(pipeline.Path, 'exists', return_value=False):
            status, msg = pipeline._pipeline_completed_today()
        assert status is Status.WARN
        assert 'no orchestrator log' in msg

    def test_handoff_runs_normally_on_trading_day(self):
        with patch('system_checks.checks.pipeline._is_trading_day', return_value=True), \
             patch.object(pipeline.Path, 'exists', return_value=False):
            status, msg = pipeline._handoff_written_today()
        assert status is Status.WARN
        assert 'no sized handoff' in msg


# ── Untouched checks must NOT have a trading-day guard ────────────────────

class TestUntouchedChecks:
    def test_geometry_check_has_no_trading_day_guard(self):
        """signals_geometry_ordered_today already SKIPs when empty; it must
        not gain a non-trading-day short-circuit (it would never reach its DB
        query in the test if guarded, but we assert by source inspection)."""
        import inspect
        src = inspect.getsource(pipeline._signals_geometry_ordered_today)
        assert '_is_trading_day' not in src

    def test_alpaca_submissions_check_has_no_trading_day_guard(self):
        import inspect
        src = inspect.getsource(pipeline._alpaca_submissions_match_handoff_today)
        assert '_is_trading_day' not in src
