"""position_ownership_clean — the B3 regression probe.

A shortfall means signals are marking a position the broker does not hold
(phantom P&L); an unallocated means shares no strategy owns are sitting in the
book. Both are findings the daily maintenance sweep must surface.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from system_checks.checks import broker  # noqa: E402
from system_checks.types import Status  # noqa: E402

TODAY = date.today()


class _Cursor:
    """Answers each query by the table/keyword it mentions."""
    def __init__(self, regclass='position_ownership', latest=TODAY, counts=None):
        self.regclass = regclass
        self.latest = latest
        self.counts = counts or {}
        self._last = ''

    def execute(self, sql, params=None):
        self._last = ' '.join(sql.split())

    def fetchone(self):
        if 'to_regclass' in self._last:
            return (self.regclass,)
        if 'MAX(cycle_date)' in self._last:
            return (self.latest,)
        if 'CURRENT_DATE' in self._last:
            return ((TODAY - self.latest).days if self.latest else 0,)
        return (None,)

    def fetchall(self):
        return list(self.counts.items())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, cur):
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub/stub')
    monkeypatch.setattr(broker.psycopg2, 'connect', lambda *a, **k: _Conn(cur))


def test_all_ok_passes(monkeypatch):
    _wire(monkeypatch, _Cursor(counts={'ok': 12}))
    status, detail = broker._position_ownership_clean()
    assert status is Status.PASS and '12' in detail


def test_unallocated_warns(monkeypatch):
    _wire(monkeypatch, _Cursor(counts={'ok': 10, 'unallocated': 2}))
    status, detail = broker._position_ownership_clean()
    assert status is Status.WARN and 'unallocated' in detail


def test_shortfall_fails(monkeypatch):
    _wire(monkeypatch, _Cursor(counts={'ok': 10, 'shortfall': 1}))
    status, detail = broker._position_ownership_clean()
    assert status is Status.FAIL and 'SHORTFALL' in detail


def test_shortfall_outranks_unallocated(monkeypatch):
    _wire(monkeypatch, _Cursor(counts={'unallocated': 5, 'shortfall': 1}))
    assert broker._position_ownership_clean()[0] is Status.FAIL


def test_missing_table_skips(monkeypatch):
    _wire(monkeypatch, _Cursor(regclass=None))
    status, detail = broker._position_ownership_clean()
    assert status is Status.SKIP and 'migrated' in detail


def test_no_rows_yet_skips(monkeypatch):
    _wire(monkeypatch, _Cursor(latest=None))
    assert broker._position_ownership_clean()[0] is Status.SKIP


def test_stale_ledger_warns(monkeypatch):
    _wire(monkeypatch, _Cursor(latest=TODAY - timedelta(days=9), counts={'ok': 3}))
    status, detail = broker._position_ownership_clean()
    assert status is Status.WARN and 'old' in detail


def test_query_failure_is_a_fail_not_a_raise(monkeypatch):
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub/stub')

    def _boom(*a, **k):
        raise RuntimeError('connection refused')
    monkeypatch.setattr(broker.psycopg2, 'connect', _boom)
    status, detail = broker._position_ownership_clean()
    assert status is Status.FAIL and 'RuntimeError' in detail


def test_check_is_registered_with_the_right_tags():
    from system_checks.registry import all_checks
    reg = all_checks()
    assert 'position_ownership_clean' in reg, 'check not registered'
    meta = reg['position_ownership_clean']
    assert 'broker' in meta['tags'] and meta['requires'] == ['db']
