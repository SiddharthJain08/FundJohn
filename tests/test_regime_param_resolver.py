"""Tests for regime_param_resolver — DB-backed param read API + cache."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import regime_param_resolver as rpr  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.executed = []

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


def test_get_row_missing_returns_none(monkeypatch):
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[]))
    rpr._cache.clear()
    assert rpr.get_row('strat_x', 'LOW_VOL') is None


def test_get_row_present_returns_dict(monkeypatch):
    row = ('strat_x', 'LOW_VOL', True, 0.5, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row]))
    rpr._cache.clear()
    r = rpr.get_row('strat_x', 'LOW_VOL')
    assert r['strategy_id'] == 'strat_x'
    assert r['regime_state'] == 'LOW_VOL'
    assert r['eligible'] is True
    assert float(r['size_scalar']) == 0.5


def test_cache_hit_skips_db(monkeypatch):
    row = ('s1', 'LOW_VOL', True, None, None, None, None)
    calls = {'n': 0}

    def factory():
        calls['n'] += 1
        return FakeConn(rows=[row] if calls['n'] == 1 else [])
    monkeypatch.setattr(rpr, '_connect', factory)
    rpr._cache.clear()
    rpr.get_row('s1', 'LOW_VOL')   # populates
    rpr.get_row('s1', 'LOW_VOL')   # cache hit
    assert calls['n'] == 1


def test_cache_invalidate_forces_refetch(monkeypatch):
    row1 = ('s1', 'LOW_VOL', True, None, None, None, None)
    row2 = ('s1', 'LOW_VOL', False, None, None, None, None)
    calls = {'n': 0}

    def factory():
        calls['n'] += 1
        return FakeConn(rows=[row1 if calls['n'] == 1 else row2])
    monkeypatch.setattr(rpr, '_connect', factory)
    rpr._cache.clear()
    assert rpr.get_row('s1', 'LOW_VOL')['eligible'] is True
    rpr.invalidate('s1', 'LOW_VOL')
    assert rpr.get_row('s1', 'LOW_VOL')['eligible'] is False
    assert calls['n'] == 2


def test_is_eligible_missing_returns_true(monkeypatch):
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[]))
    rpr._cache.clear()
    assert rpr.is_eligible('unmigrated_strategy', 'LOW_VOL') is True


def test_is_eligible_false_when_row_false(monkeypatch):
    row = ('s1', 'HIGH_VOL', False, None, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row]))
    rpr._cache.clear()
    assert rpr.is_eligible('s1', 'HIGH_VOL') is False


def test_size_scalar_null_returns_phase1_default(monkeypatch):
    row = ('s1', 'LOW_VOL', True, None, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row]))
    rpr._cache.clear()
    assert rpr.size_scalar('s1', 'LOW_VOL') == 1.0
    rpr._cache.clear()
    row2 = ('s1', 'TRANSITIONING', True, None, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row2]))
    assert rpr.size_scalar('s1', 'TRANSITIONING') == 0.55
    rpr._cache.clear()
    row3 = ('s1', 'HIGH_VOL', True, None, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row3]))
    assert rpr.size_scalar('s1', 'HIGH_VOL') == 0.35
    rpr._cache.clear()
    row4 = ('s1', 'CRISIS', True, None, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row4]))
    assert rpr.size_scalar('s1', 'CRISIS') == 0.15


def test_size_scalar_non_null_overrides(monkeypatch):
    row = ('s1', 'HIGH_VOL', True, 0.5, None, None, None)
    monkeypatch.setattr(rpr, '_connect', lambda: FakeConn(rows=[row]))
    rpr._cache.clear()
    assert rpr.size_scalar('s1', 'HIGH_VOL') == 0.5
