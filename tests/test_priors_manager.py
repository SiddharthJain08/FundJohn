"""Tests for priors_manager — strategy_regime_priors upsert + audit."""
from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import priors_manager as pm  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
        self._queue = list(rows or [])
        self.executed: list = []
        self.rowcount = 0

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=()): self.executed.append((sql, params))
    def fetchone(self):
        return self._queue.pop(0) if self._queue else None
    def fetchall(self):
        return list(self._queue)


class FakeConn:
    def __init__(self, rows=()):
        self.cur = FakeCursor(rows)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def cursor(self): return self.cur
    def commit(self): pass


def test_set_prior_writes_audit_then_upserts(monkeypatch):
    # _lock returns the existing row (None on first set)
    conn = FakeConn(rows=[None])  # FOR UPDATE result
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    pm.set_prior(strategy_id='s1', regime_state='LOW_VOL',
                 expected_sharpe=1.5, expected_win_rate=0.6,
                 expected_avg_pnl_pct=0.012,
                 source='Asness 2013', confidence=0.9,
                 notes='canonical momentum prior', actor='operator:t')
    sql_starts = [e[0].strip().split()[0].upper() for e in conn.cur.executed]
    assert sql_starts == ['SELECT', 'INSERT', 'INSERT']
    assert 'strategy_regime_priors_changes' in conn.cur.executed[1][0]
    assert 'strategy_regime_priors' in conn.cur.executed[2][0]
    assert 'ON CONFLICT' in conn.cur.executed[2][0]


def test_set_prior_rejects_invalid_regime():
    with pytest.raises(ValueError, match='invalid regime'):
        pm.set_prior(strategy_id='s1', regime_state='BOGUS',
                     expected_sharpe=1.0, expected_win_rate=None,
                     expected_avg_pnl_pct=None,
                     source='t', confidence=None, notes=None,
                     actor='operator:t')


def test_set_prior_requires_source():
    with pytest.raises(ValueError, match='source'):
        pm.set_prior(strategy_id='s1', regime_state='LOW_VOL',
                     expected_sharpe=1.0, expected_win_rate=None,
                     expected_avg_pnl_pct=None,
                     source='', confidence=None, notes=None,
                     actor='operator:t')


def test_list_priors_returns_rows(monkeypatch):
    rows = [
        ('s1', 'LOW_VOL', 1.5, 0.6, 0.012, 'Asness 2013', 0.9, 'notes', None, 'operator:t'),
    ]
    class C:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=()): pass
        def fetchall(self): return rows
        @property
        def description(self):
            cols = ('strategy_id', 'regime_state', 'expected_sharpe',
                    'expected_win_rate', 'expected_avg_pnl_pct', 'source',
                    'confidence', 'notes', 'set_at', 'set_by')
            return [type('C', (), {'name': c}) for c in cols]
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def cursor(self): return C()
        def commit(self): pass
    monkeypatch.setattr(pm, '_connect', lambda: Conn())
    out = pm.list_priors()
    assert len(out) == 1
    assert out[0]['strategy_id'] == 's1'


def test_get_prior_returns_none_when_missing(monkeypatch):
    conn = FakeConn(rows=[None])
    monkeypatch.setattr(pm, '_connect', lambda: conn)
    assert pm.get_prior('does_not_exist', 'LOW_VOL') is None
