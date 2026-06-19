"""tests/test_unreconciled_submissions_backlog.py

Unit tests for the `unreconciled_submissions_backlog` system_check
(src/system_checks/checks/broker.py). The check surfaces alpaca_submissions
rows from PRIOR trading days that were submitted to the broker
(alpaca_order_id IS NOT NULL) but never got reconciled (broker_status IS NULL).

Background: ext-hours fills lag Alpaca's activity API by more than the 30s
reconcile poll window, so those rows stay broker_status=NULL forever and are
never re-swept. 2026-06-17 had 17/19 and 2026-06-18 had 15/17 rows stuck.
No check surfaced the backlog, so it stayed silent — this check is the
regression probe.

These tests patch psycopg2.connect so no live DB is required; we drive the
check's single COUNT/MIN query via a fake cursor.

Run:
    pytest tests/test_unreconciled_submissions_backlog.py -v
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from system_checks.types import Status  # noqa: E402
import system_checks.checks.broker as broker_checks  # noqa: E402


class _FakeCursor:
    """Replays one fetchone() result and records executed SQL for assertions."""
    def __init__(self, fetchone_result):
        self._fetchone = fetchone_result
        self.executed = []  # list of (sql, params)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, fetchone_result):
        self.cur = _FakeCursor(fetchone_result)

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def _run_with_db(fetchone_result, monkeypatch):
    """Invoke the registered check function directly with psycopg2.connect
    patched. We call the underlying function (not via run_one) so we bypass
    the runner's requires=['db'] dep-skip — but we still set POSTGRES_URI so
    the check's own guard passes."""
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub')
    conn = _FakeConn(fetchone_result)
    with patch('system_checks.checks.broker.psycopg2.connect', return_value=conn):
        fn = broker_checks._unreconciled_submissions_backlog
        return fn(), conn


def test_zero_backlog_passes(monkeypatch):
    # (count, oldest_date) — no stuck rows
    (status, detail), _ = _run_with_db((0, None), monkeypatch)
    assert status is Status.PASS, detail
    assert '0' in detail


def test_small_backlog_warns(monkeypatch):
    (status, detail), _ = _run_with_db((3, date(2026, 6, 18)), monkeypatch)
    assert status is Status.WARN, detail
    assert '3' in detail
    assert '2026-06-18' in detail  # names oldest affected date


def test_large_backlog_fails(monkeypatch):
    # Mirrors the real incident: ~15-17 stuck rows/day.
    (status, detail), _ = _run_with_db((17, date(2026, 6, 17)), monkeypatch)
    assert status is Status.FAIL, detail
    assert '17' in detail
    assert '2026-06-17' in detail


def test_query_scopes_to_prior_days_and_submitted_orders(monkeypatch):
    """The query MUST scope to broker_status IS NULL AND alpaca_order_id IS
    NOT NULL AND run_date < CURRENT_DATE — matching the sweep predicate and
    the existing alpaca_submissions_unreconciled_idx partial index. Counting
    alpaca_order_id IS NULL rows (submit errors) would make the check FAIL
    forever on rows nothing can reconcile."""
    (_, _), conn = _run_with_db((0, None), monkeypatch)
    sql = conn.cur.executed[0][0]
    norm = ' '.join(sql.split())
    assert 'broker_status IS NULL' in norm
    assert 'alpaca_order_id IS NOT NULL' in norm
    assert 'run_date < CURRENT_DATE' in norm
    assert 'alpaca_submissions' in norm


def test_no_postgres_uri_skips(monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    status, detail = broker_checks._unreconciled_submissions_backlog()
    assert status is Status.SKIP, detail


def test_db_error_returns_fail(monkeypatch):
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub')
    with patch('system_checks.checks.broker.psycopg2.connect',
               side_effect=RuntimeError('boom')):
        status, detail = broker_checks._unreconciled_submissions_backlog()
    assert status is Status.FAIL, detail


def test_registered_with_correct_tags_and_requires():
    from system_checks.registry import get
    meta = get('unreconciled_submissions_backlog')
    assert 'broker' in meta['tags']
    assert 'pipeline' in meta['tags']
    assert meta['requires'] == ['db']
