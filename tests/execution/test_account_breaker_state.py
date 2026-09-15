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


def test_breaching_shadow_line_is_byte_exact_with_flatten_tail():
    """Fix round 1, item 1: format_line appends flatten_partial=<n> at the
    end when a flatten dict is given. main() only builds and passes a
    flatten dict on a tick where a rule breach was evaluated — SHADOW mode
    still calls flatten_alpha (with journal=False, to compute the counts
    without writing) on a BREACHING tick. (Fix round 2, nit: this is no
    longer the only shadow-line test — see
    test_clean_shadow_line_is_byte_exact_without_flatten_tail below for the
    non-breach case, where the plan's main() passes flat=None and
    format_line must omit the tail entirely; the previous single test here
    paired a non-breaching ST_CLEAN with a flatten dict, which main() never
    actually does.)"""
    line = ab.format_line('shadow', equity=190_100.0, bench_mv=41_000.0,
                          alpha=149_100.0, st=ST_BREACH, open_equity=205_000.0,
                          open_src='estimated', halted=False,
                          flatten={'ok': 2, 'fail': 0, 'partial': 0, 'pending': False})
    assert line == (
        '[account_breaker] shadow equity=190100.00 bench_mv=41000.00 '
        'alpha_nav=149100.00 peak=171200.00 dd=-0.1291 open_equity=205000.00 '
        'open_src=estimated daily=-0.0727 rule=drawdown breach=1 halted=0 '
        'flatten_ok=2 flatten_fail=0 pending=0 flatten_partial=0')


def test_clean_shadow_line_is_byte_exact_without_flatten_tail():
    """Fix round 2, nit: restores the pre-fix-round-1 byte-exact coverage for
    the ordinary, non-breach tick. The plan's main() passes flat=None (it
    never calls flatten_alpha at all when nothing breached), so format_line
    must render exactly the base line with NO flatten_* tokens — not
    flatten_partial=0, not a trailing space, nothing."""
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
                          flatten={'ok': 6, 'fail': 1, 'partial': 2, 'pending': True})
    assert line.endswith('rule=drawdown breach=1 halted=1 '
                         'flatten_ok=6 flatten_fail=1 pending=1 flatten_partial=2')
    assert line.startswith('[account_breaker] armed ')


def test_line_renders_a_missing_daily_as_na():
    st = dict(ST_CLEAN, daily=None)
    line = ab.format_line('shadow', equity=1.0, bench_mv=0.0, alpha=1.0, st=st,
                          open_equity=0.0, open_src='equity', halted=False)
    assert ' daily=n/a ' in line


def test_line_renders_a_missing_peak_and_dd_as_na():
    """Supplement item 5 (task 7): format_line must never raise on a None
    peak/dd — the line has to be emitted on EVERY tick, so a defensive None
    here (e.g. a future caller that hasn't seeded a peak yet) must not take
    the process down."""
    st = dict(ST_CLEAN, peak=None, dd=None)
    line = ab.format_line('shadow', equity=1.0, bench_mv=0.0, alpha=1.0, st=st,
                          open_equity=0.0, open_src='equity', halted=False)
    assert ' peak=n/a dd=n/a ' in line


# ── return values (supplement item 2) ────────────────────────────────────────

def test_save_state_returns_true_on_success():
    cur = FakeCursor()
    assert ab.save_state(cur, halted=False, reason=None, breached_at=None,
                         peak=1.0, dd=-0.01, daily=None, pending_flatten=False) is True


def test_clear_halt_returns_true_on_success():
    cur = FakeCursor()
    assert ab.clear_halt(cur, 100_000.0) is True


def test_clear_halt_resets_flatten_attempts_to_zero_as_a_literal():
    """The re-arm must not inherit a stale retry count from the halt it just
    cleared, and MUST do so as a SQL literal (not a bound %s param) so it
    never disturbs the existing `params[0] == alpha` contract other tests
    already rely on."""
    cur = FakeCursor()
    ab.clear_halt(cur, 149_100.0)
    (sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert 'flatten_attempts = 0' in sql
    assert params == (149_100.0,)


# ── ON CONFLICT / Z-spelling pins (supplement item 3) ────────────────────────

def test_opening_equity_insert_has_on_conflict_do_nothing(tmp_path):
    cur = FakeCursor([None])
    path = _ohlc(tmp_path, {'2026-09-16': {'open': 205_000.0}})
    ab.opening_equity(cur, SESSION, 190_000.0, path=path)
    (sql, _params), = cur.sql_matching('INSERT INTO account_daily_open')
    assert 'ON CONFLICT (session_date) DO NOTHING' in sql


def test_iso_variants_include_the_z_spellings():
    variants = ab._iso_variants(BREACHED)
    assert '2026-09-16T17:42:11Z' in variants
    assert '2026-09-16T17:42:11+00:00' in variants


def test_rearm_accepts_the_z_spelling(monkeypatch):
    monkeypatch.setenv(ab.REARM_ENV, '2026-09-16T17:42:11Z')
    assert ab.rearm_requested(_halted()) is True


# ── fails-open under a raising cursor (supplement item 3) ────────────────────

class RaisingCursor:
    """SAVEPOINT/RELEASE/ROLLBACK succeed; a named payload statement raises —
    proves every read/write here fails OPEN under a real Postgres error
    without poisoning the caller's transaction."""

    def __init__(self, raise_needle, rows=None):
        self._raise_needle = raise_needle
        self._rows = list(rows or [])
        self.calls = []

    def execute(self, sql, params=None):
        flat = ' '.join(sql.split())
        self.calls.append((flat, params))
        if self._raise_needle in flat:
            raise RuntimeError('db down')

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        out, self._rows = list(self._rows), []
        return out

    def sql_matching(self, needle):
        return [c for c in self.calls if needle in c[0]]


def test_load_state_raising_cursor_fails_open_and_rolls_back():
    cur = RaisingCursor('SELECT halted')
    assert ab.load_state(cur) == ab._EMPTY_STATE
    assert cur.sql_matching('ROLLBACK TO SAVEPOINT') != []


def test_opening_equity_stored_read_failure_falls_back_to_ohlc(tmp_path):
    cur = RaisingCursor('SELECT opening_equity')
    path = _ohlc(tmp_path, {'2026-09-16': {'open': 205_000.0}})
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=path)
    assert (value, src) == (205_000.0, 'ohlc')


def test_opening_equity_stored_read_failure_falls_back_to_equity(tmp_path):
    cur = RaisingCursor('SELECT opening_equity')
    path = _ohlc(tmp_path, {})
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=path)
    assert (value, src) == (190_000.0, 'equity')


def test_opening_equity_insert_failure_still_returns_a_value(tmp_path):
    cur = RaisingCursor('INSERT INTO account_daily_open')
    path = _ohlc(tmp_path, {'2026-09-16': {'open': 205_000.0}})
    value, src = ab.opening_equity(cur, SESSION, 190_000.0, path=path)
    assert (value, src) == (205_000.0, 'ohlc')


def test_save_state_raising_cursor_returns_false_not_raise():
    cur = RaisingCursor('UPDATE account_breaker_state')
    assert ab.save_state(cur, halted=True, reason='drawdown', breached_at=None,
                         peak=1.0, dd=-0.1, daily=None, pending_flatten=True) is False


def test_clear_halt_raising_cursor_returns_false_not_raise():
    cur = RaisingCursor('UPDATE account_breaker_state')
    assert ab.clear_halt(cur, 100_000.0) is False


def test_dead_cursor_savepoint_itself_raises_no_propagation():
    """SAVEPOINT itself raising (a genuinely dead cursor/connection) must
    still not propagate — the ROLLBACK/RELEASE attempts in the except branch
    also raise here and are swallowed too."""
    cur = RaisingCursor('SAVEPOINT')
    assert ab.load_state(cur) == ab._EMPTY_STATE


# ── flatten_attempts (F-5, additive column, migration 158) ──────────────────

def test_load_flatten_attempts_defaults_to_zero_on_a_missing_row():
    assert ab.load_flatten_attempts(FakeCursor([])) == 0


def test_load_flatten_attempts_reads_the_stored_value():
    cur = FakeCursor([(4,)])
    assert ab.load_flatten_attempts(cur) == 4


def test_load_flatten_attempts_fails_open_to_zero_on_a_raising_cursor():
    cur = RaisingCursor('SELECT flatten_attempts')
    assert ab.load_flatten_attempts(cur) == 0


def test_save_state_flatten_attempts_defaults_to_untouched():
    """flatten_attempts=None (the default) must COALESCE to the stored
    value, not clobber it — only the two run_once() call sites that attempt
    a flatten pass an explicit new count."""
    cur = FakeCursor()
    ab.save_state(cur, halted=False, reason=None, breached_at=None,
                  peak=1.0, dd=-0.01, daily=None, pending_flatten=False)
    (sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert 'COALESCE' in sql
    assert params[-1] is None


def test_save_state_flatten_attempts_explicit_value_is_persisted():
    cur = FakeCursor()
    ab.save_state(cur, halted=True, reason='drawdown', breached_at=None,
                  peak=1.0, dd=-0.1, daily=None, pending_flatten=True,
                  flatten_attempts=3)
    (_sql, params), = cur.sql_matching('UPDATE account_breaker_state')
    assert params[-1] == 3
