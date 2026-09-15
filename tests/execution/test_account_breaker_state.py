"""C1 state layer: persistence, the opening-equity snapshot, the operator
re-arm token, and the exact shadow/armed line the operator greps.

All DB access goes through a fake cursor; the OHLC store is written into
tmp_path. No Postgres, no CLI, no network.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import account_breaker as ab  # noqa: E402


class FakeCursor:
    """Replays queued rows in order and records every (sql, params) pair."""

    def __init__(self, rows=None):
        self._rows = list(rows or [])
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        out, self._rows = list(self._rows), []
        return out

    def sql_matching(self, needle):
        return [c for c in self.calls if needle in c[0]]


BREACHED = datetime(2026, 9, 16, 17, 42, 11, tzinfo=timezone.utc)


# ── load_state / save_state ─────────────────────────────────────────────────

def test_load_state_maps_the_singleton_row():
    cur = FakeCursor([(True, 'drawdown', BREACHED, 171_200.0, -0.129, -0.072, True)])
    st = ab.load_state(cur)
    assert st == {'halted': True, 'reason': 'drawdown', 'breached_at': BREACHED,
                  'peak': 171_200.0, 'dd': -0.129, 'daily': -0.072,
                  'pending_flatten': True}


def test_load_state_missing_row_is_a_clean_default():
    st = ab.load_state(FakeCursor([]))
    assert st['halted'] is False and st['peak'] is None and st['breached_at'] is None


def test_save_state_updates_the_singleton_only():
    cur = FakeCursor()
    ab.save_state(cur, halted=False, reason=None, breached_at=None,
                  peak=171_200.0, dd=-0.01, daily=-0.002, pending_flatten=False)
    (sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert 'WHERE id = 1' in sql
    assert params[3] == 171_200.0


# ── opening_equity ──────────────────────────────────────────────────────────

SESSION = date(2026, 9, 16)


def _ohlc(tmp_path, days):
    p = tmp_path / 'pnl_daily_ohlc.json'
    p.write_text(json.dumps({'days': days}))
    return p


def test_opening_equity_prefers_the_stored_row():
    cur = FakeCursor([(205_000.0,)])
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=Path('/nonexistent'))
    assert (value, src) == (205_000.0, 'stored')
    assert cur.sql_matching('INSERT INTO account_daily_open') == []


def test_opening_equity_falls_back_to_todays_ohlc_open(tmp_path):
    cur = FakeCursor([None])
    path = _ohlc(tmp_path, {'2026-09-16': {'open': 205_000.0, 'high': 206_000.0,
                                           'low': 189_000.0, 'close': 190_000.0}})
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=path)
    assert (value, src) == (205_000.0, 'ohlc')
    (_sql, params), = cur.sql_matching('INSERT INTO account_daily_open')
    assert params == (SESSION, 205_000.0, False)      # ohlc open is NOT estimated


def test_opening_equity_reconstructs_from_current_equity_and_marks_estimated(tmp_path):
    cur = FakeCursor([None])
    path = _ohlc(tmp_path, {})
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=path)
    assert (value, src) == (190_000.0, 'equity')
    (_sql, params), = cur.sql_matching('INSERT INTO account_daily_open')
    assert params == (SESSION, 190_000.0, True)


def test_opening_equity_unreadable_store_still_returns_a_value(tmp_path):
    cur = FakeCursor([None])
    bad = tmp_path / 'broken.json'
    bad.write_text('{not json')
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=bad)
    assert (value, src) == (190_000.0, 'equity')


# ── re-arm token ────────────────────────────────────────────────────────────

def _halted(breached_at=BREACHED):
    return {'halted': True, 'reason': 'drawdown', 'breached_at': breached_at,
            'peak': 171_200.0, 'dd': -0.13, 'daily': -0.07, 'pending_flatten': False}


def test_rearm_requires_the_exact_breached_at_token(monkeypatch):
    monkeypatch.setenv(ab.REARM_ENV, BREACHED.isoformat())
    assert ab.rearm_requested(_halted()) is True


def test_rearm_accepts_the_second_precision_spelling(monkeypatch):
    monkeypatch.setenv(ab.REARM_ENV, '2026-09-16T17:42:11+00:00')
    assert ab.rearm_requested(_halted()) is True


def test_a_stale_token_cannot_clear_a_later_breach(monkeypatch):
    monkeypatch.setenv(ab.REARM_ENV, '2026-08-01T14:00:00+00:00')
    assert ab.rearm_requested(_halted()) is False


def test_rearm_is_ignored_when_not_halted(monkeypatch):
    monkeypatch.setenv(ab.REARM_ENV, BREACHED.isoformat())
    st = _halted()
    st['halted'] = False
    assert ab.rearm_requested(st) is False


def test_rearm_absent_token_is_false(monkeypatch):
    monkeypatch.delenv(ab.REARM_ENV, raising=False)
    assert ab.rearm_requested(_halted()) is False


def test_clear_halt_resets_the_peak_to_current_alpha_nav():
    cur = FakeCursor()
    ab.clear_halt(cur, 149_100.0)
    (sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert 'halted = FALSE' in sql and 'rearmed_at = NOW()' in sql
    assert params[0] == 149_100.0


# ── the grep contract ───────────────────────────────────────────────────────

ST_CLEAN = {'peak': 171_200.0, 'dd': -0.0529, 'daily': -0.0090,
            'rule': 'none', 'breach': False}
ST_BREACH = {'peak': 171_200.0, 'dd': -0.1291, 'daily': -0.0727,
             'rule': 'drawdown', 'breach': True}


def test_shadow_line_is_byte_exact():
    line = ab.format_line('shadow', equity=203_145.22, bench_mv=41_000.0,
                          alpha=162_145.22, st=ST_CLEAN, open_equity=205_000.0,
                          open_src='stored', halted=False)
    assert line == (
        '[account_breaker] shadow equity=203145.22 bench_mv=41000.00 '
        'alpha_nav=162145.22 peak=171200.00 dd=-0.0529 open_equity=205000.00 '
        'open_src=stored daily=-0.0090 rule=none breach=0 halted=0')


def test_armed_line_carries_the_flatten_tail():
    line = ab.format_line('armed', equity=190_100.0, bench_mv=41_000.0,
                          alpha=149_100.0, st=ST_BREACH, open_equity=205_000.0,
                          open_src='estimated', halted=True,
                          flatten={'ok': 6, 'fail': 1, 'pending': True})
    assert line.endswith('rule=drawdown breach=1 halted=1 '
                         'flatten_ok=6 flatten_fail=1 pending=1')
    assert line.startswith('[account_breaker] armed ')


def test_line_renders_a_missing_daily_as_na():
    st = dict(ST_CLEAN, daily=None)
    line = ab.format_line('shadow', equity=1.0, bench_mv=0.0, alpha=1.0, st=st,
                          open_equity=0.0, open_src='equity', halted=False)
    assert ' daily=n/a ' in line
