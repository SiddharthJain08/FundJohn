"""Tests for the __NULL__ sentinel in eligibility_manager.set_params."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import eligibility_manager as em  # noqa: E402


class FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)
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


def test_null_sentinel_resets_size_scalar(monkeypatch):
    """Passing '__NULL__' for size_scalar must produce a SQL NULL upsert."""
    existing = ('s1', 'LOW_VOL', True, 0.5, None, None, None)
    conn = FakeConn(rows=[existing])
    monkeypatch.setattr(em, '_connect', lambda: conn)
    monkeypatch.setattr(em, '_invalidate_cache', lambda sid, r: None)
    em.set_params(strategy_id='s1', regime_state='LOW_VOL',
                  size_scalar=em.NULL_SENTINEL,
                  actor='operator:t', reason='reset back to default',
                  source='cli')
    upsert_params = conn.cur.executed[2][1]
    # 8 params: strategy_id, regime, eligible, size, stop, target, max_hold, set_by
    assert upsert_params[3] is None     # size_scalar reset to NULL
    assert upsert_params[2] is True    # eligible unchanged


def test_null_sentinel_only_affects_targeted_columns(monkeypatch):
    """Other columns must remain unchanged when only size_scalar uses sentinel."""
    existing = ('s1', 'LOW_VOL', True, 0.5, 0.02, 0.05, 10)
    conn = FakeConn(rows=[existing])
    monkeypatch.setattr(em, '_connect', lambda: conn)
    monkeypatch.setattr(em, '_invalidate_cache', lambda sid, r: None)
    em.set_params(strategy_id='s1', regime_state='LOW_VOL',
                  size_scalar=em.NULL_SENTINEL,
                  actor='operator:t', reason='', source='cli')
    upsert_params = conn.cur.executed[2][1]
    assert upsert_params[3] is None        # size_scalar reset
    assert upsert_params[4] == 0.02        # stop_pct preserved
    assert upsert_params[5] == 0.05        # target_pct preserved
    assert upsert_params[6] == 10           # max_hold_days preserved


def test_null_sentinel_works_for_all_numeric_columns(monkeypatch):
    """Sentinel accepted for size_scalar, stop_pct, target_pct, max_hold_days."""
    existing = ('s1', 'LOW_VOL', True, 0.5, 0.02, 0.05, 10)
    conn = FakeConn(rows=[existing])
    monkeypatch.setattr(em, '_connect', lambda: conn)
    monkeypatch.setattr(em, '_invalidate_cache', lambda sid, r: None)
    em.set_params(strategy_id='s1', regime_state='LOW_VOL',
                  stop_pct=em.NULL_SENTINEL,
                  target_pct=em.NULL_SENTINEL,
                  max_hold_days=em.NULL_SENTINEL,
                  actor='operator:t', reason='', source='cli')
    upsert_params = conn.cur.executed[2][1]
    assert upsert_params[3] == 0.5    # size_scalar preserved
    assert upsert_params[4] is None   # stop_pct reset
    assert upsert_params[5] is None   # target_pct reset
    assert upsert_params[6] is None   # max_hold_days reset
