"""tests/test_regime_freshness_db_check.py

system_checks regime_freshness_db, retargeted 2026-06-08. The intraday HMM is
the sole regime authority and writes intraday_regime_states every 5 min during
9:00-19:55 ET Mon-Fri (incl. carry-forward ticks). market_regime now only gets
a row on a confirmed transition, so its row-age stopped being a freshness
signal — this check measures the intraday producer's liveness instead.

Thresholds sit above the max expected weekend gap (Fri 19:55 ET -> Mon 09:00 ET
~= 61h) and below the engine's 80h stale-gate: WARN >66h, FAIL >72h. So a
normal weekend never false-alarms, but a multi-day producer death does.

Run:
    pytest tests/test_regime_freshness_db_check.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from system_checks.checks import regime as scregime  # noqa: E402
from system_checks.types import Status  # noqa: E402


def _mock_pg(rows):
    """rows: the value cur.fetchone() returns (tuple or None)."""
    cur = MagicMock()
    cur.fetchone.return_value = rows
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return lambda: conn


def _row(age_hours, state='LOW_VOL'):
    return (state, datetime.now(timezone.utc) - timedelta(hours=age_hours))


class TestRegimeFreshnessDb:
    def test_fresh_tick_passes(self, monkeypatch):
        monkeypatch.setattr(scregime, '_pg', _mock_pg(_row(0.1)))
        status, _ = scregime._regime_freshness_db()
        assert status == Status.PASS

    def test_normal_weekend_gap_passes(self, monkeypatch):
        """Fri 19:55 ET -> Mon 09:00 ET ~= 61h must NOT alarm (it would under
        the old 26h market_regime threshold)."""
        monkeypatch.setattr(scregime, '_pg', _mock_pg(_row(61)))
        status, _ = scregime._regime_freshness_db()
        assert status == Status.PASS

    def test_warn_window(self, monkeypatch):
        monkeypatch.setattr(scregime, '_pg', _mock_pg(_row(68)))   # >66, <72
        status, _ = scregime._regime_freshness_db()
        assert status == Status.WARN

    def test_stalled_producer_fails(self, monkeypatch):
        monkeypatch.setattr(scregime, '_pg', _mock_pg(_row(75)))   # >72
        status, _ = scregime._regime_freshness_db()
        assert status == Status.FAIL

    def test_no_rows_fails(self, monkeypatch):
        monkeypatch.setattr(scregime, '_pg', _mock_pg(None))
        status, msg = scregime._regime_freshness_db()
        assert status == Status.FAIL
        assert 'intraday_regime_states' in msg
