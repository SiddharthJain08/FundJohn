"""Fix 3 — the 'pipeline ran today' checks must SKIP on non-trading days.

On NYSE holidays (e.g. Juneteenth 2026-06-19) and weekends no pipeline runs,
so pipeline_completed_today / signals_persisted_today / handoff_written_today /
carried_set_present WARN/FAIL spuriously. They must SKIP first.

_is_trading_day(d) delegates to lib.trading_calendar.is_session: master-first,
then one `alpaca calendar --start --end` probe, then weekday arithmetic with a
WARNING — the library owns that whole fallback chain (task 3c), so this file
no longer mocks subprocess directly; it exercises the library's own tiers via
lib.trading_calendar's public monkeypatch seams (the library's own test suite
covers the CLI's exact timeout/nonzero-rc/bad-json shapes).
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import trading_calendar as tc  # noqa: E402
from system_checks.checks import pipeline  # noqa: E402
from system_checks.types import Status  # noqa: E402


# ── _is_trading_day unit tests (library contract) ────────────────────────

class TestIsTradingDay:
    def test_master_governs_trading_day(self, tmp_path, monkeypatch):
        """Master-first: a weekday session, the Juneteenth holiday, and the
        following Saturday all resolve from the master — no CLI probe."""
        rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
                for d in pd.bdate_range('2026-06-01', '2026-06-30')
                if d.date() != date(2026, 6, 19)]
        p = tmp_path / 'cal.parquet'
        pd.DataFrame(rows).to_parquet(p, index=False)
        monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
        tc.clear_cache()
        monkeypatch.setattr(
            tc, '_alpaca_sessions',
            lambda a, b: (_ for _ in ()).throw(AssertionError('alpaca probe must not run')))
        try:
            assert pipeline._is_trading_day(date(2026, 6, 18)) is True
            assert pipeline._is_trading_day(date(2026, 6, 19)) is False   # Juneteenth
            assert pipeline._is_trading_day(date(2026, 6, 20)) is False  # Saturday
        finally:
            tc.clear_cache()

    def test_cli_failure_falls_back_to_weekday_with_warning(self, monkeypatch, caplog):
        """No master, alpaca probe fails -> weekday arithmetic (holiday-blind),
        with a WARNING logged. Collapses the old timeout/nonzero-rc/bad-json
        variants into one test — those CLI shapes are the library's own to
        cover (src/lib/trading_calendar.py)."""
        monkeypatch.setenv(tc.MASTER_PATH_ENV, '/nonexistent/no_such_calendar.parquet')
        tc.clear_cache()
        monkeypatch.setattr(tc, '_alpaca_sessions', lambda a, b: None)
        try:
            with caplog.at_level(logging.WARNING, logger='lib.trading_calendar'):
                assert pipeline._is_trading_day(date(2026, 6, 18)) is True   # Thu
                assert pipeline._is_trading_day(date(2026, 6, 20)) is False  # Sat
            assert any('weekday fallback' in r.message for r in caplog.records)
        finally:
            tc.clear_cache()


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
