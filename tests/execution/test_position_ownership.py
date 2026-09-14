"""B3 (spec item 15): do the shares the broker holds match what signals claim?

unknown = account_qty - signal_qty on SIGNED quantities, so a short book reads
the same way a long one does. Tolerances are asymmetric — a shortfall (signals
marking a position the broker doesn't hold) is the dangerous direction — and
floored at 1 share so rounding dust is never a finding.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import position_ownership as po  # noqa: E402


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1


# ── classify ───────────────────────────────────────────────────────────────

def test_exact_match_is_ok():
    assert po.classify(100.0, 100.0) == (0.0, po.STATUS_OK)


def test_one_share_of_dust_is_ok_in_both_directions():
    assert po.classify(101.0, 100.0)[1] == po.STATUS_OK
    assert po.classify(99.0, 100.0)[1] == po.STATUS_OK


def test_broker_extra_beyond_tolerance_is_unallocated():
    unknown, status = po.classify(10_000.0, 9_000.0)
    assert unknown == 1000.0 and status == po.STATUS_UNALLOCATED


def test_signals_claiming_more_than_the_broker_holds_is_shortfall():
    unknown, status = po.classify(9_000.0, 10_000.0)
    assert unknown == -1000.0 and status == po.STATUS_SHORTFALL


def test_percentage_tolerances_are_asymmetric():
    # 10,000 share position: 0.1% extra (10 sh) ok, 0.5% shortfall (50 sh) ok.
    assert po.classify(10_010.0, 10_000.0)[1] == po.STATUS_OK
    assert po.classify(10_011.0, 10_000.0)[1] == po.STATUS_UNALLOCATED
    assert po.classify(9_950.0, 10_000.0)[1] == po.STATUS_OK
    assert po.classify(9_949.0, 10_000.0)[1] == po.STATUS_SHORTFALL


def test_short_book_uses_the_same_signed_rule():
    assert po.classify(-100.0, -100.0)[1] == po.STATUS_OK
    # broker is shorter than the ledger claims -> extra short exposure nobody owns
    assert po.classify(-10_000.0, -9_000.0)[1] == po.STATUS_SHORTFALL
    assert po.classify(-9_000.0, -10_000.0)[1] == po.STATUS_UNALLOCATED


# ── compute_ownership ──────────────────────────────────────────────────────

def test_broker_only_ticker_is_unallocated():
    rows = {r['ticker']: r for r in po.compute_ownership({'ZZZ': 500.0}, {})}
    assert rows['ZZZ']['status'] == po.STATUS_UNALLOCATED
    assert rows['ZZZ']['signal_qty'] == 0.0


def test_signal_only_ticker_is_shortfall():
    rows = {r['ticker']: r for r in po.compute_ownership({}, {'YYY': 500.0})}
    assert rows['YYY']['status'] == po.STATUS_SHORTFALL
    assert rows['YYY']['account_qty'] == 0.0


def test_compute_ownership_covers_the_union_and_sorts():
    rows = po.compute_ownership({'BBB': 1.0, 'AAA': 1.0}, {'CCC': 1.0})
    assert [r['ticker'] for r in rows] == ['AAA', 'BBB', 'CCC']


# ── transitions ────────────────────────────────────────────────────────────

def test_transitions_reports_only_changes():
    rows = po.compute_ownership({'AAA': 100.0, 'BBB': 500.0}, {'AAA': 100.0, 'BBB': 0.0})
    lines = po.transitions(rows, {'AAA': po.STATUS_OK, 'BBB': po.STATUS_UNALLOCATED})
    assert lines == []


def test_transitions_reports_a_new_problem():
    rows = po.compute_ownership({'BBB': 500.0}, {'BBB': 0.0})
    lines = po.transitions(rows, {'BBB': po.STATUS_OK})
    assert len(lines) == 1 and 'ok -> unallocated' in lines[0] and 'BBB' in lines[0]


def test_transitions_reports_a_recovery():
    rows = po.compute_ownership({'BBB': 100.0}, {'BBB': 100.0})
    lines = po.transitions(rows, {'BBB': po.STATUS_SHORTFALL})
    assert len(lines) == 1 and 'shortfall -> ok' in lines[0]


def test_transitions_skips_a_first_sighting_of_a_healthy_ticker():
    rows = po.compute_ownership({'NEW': 100.0}, {'NEW': 100.0})
    assert po.transitions(rows, {}) == []


def test_transitions_reports_a_first_sighting_of_an_unhealthy_ticker():
    rows = po.compute_ownership({'NEW': 500.0}, {})
    lines = po.transitions(rows, {})
    assert len(lines) == 1 and 'new -> unallocated' in lines[0]


# ── loaders + pass ─────────────────────────────────────────────────────────

def test_load_account_qty_sums_signed_share_counts():
    positions = [{'symbol': 'AAA', 'qty': '100'}, {'symbol': 'BBB', 'qty': '-50'}]
    assert po.load_account_qty(lambda: positions) == {'AAA': 100.0, 'BBB': -50.0}


def test_load_account_qty_returns_none_when_the_broker_is_unreadable():
    assert po.load_account_qty(lambda: None) is None


def test_persist_ownership_is_append_only():
    cur = _Cursor()
    rows = po.compute_ownership({'AAA': 100.0}, {'AAA': 100.0})
    assert po.persist_ownership(cur, date(2026, 9, 15), rows) == 1
    sql, params = cur.calls[0]
    assert sql.startswith('INSERT INTO position_ownership')
    assert 'ON CONFLICT (cycle_date, ticker) DO NOTHING' in sql
    assert params[1] == 'AAA' and params[5] == po.STATUS_OK


def test_run_ownership_pass_skips_when_the_broker_is_unreadable(monkeypatch):
    # account_qty defaults to None, so the pass calls load_account_qty; stub it
    # to the "couldn't ask the broker" answer.
    monkeypatch.setattr(po, 'load_account_qty', lambda *a, **k: None)
    out = po.run_ownership_pass(_Conn(_Cursor()), date(2026, 9, 15),
                                log_fn=lambda m: None)
    assert out == {'skipped': 'broker_unavailable'}


def test_run_ownership_pass_counts_and_never_raises(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(po, 'load_signal_qty', lambda c: {'AAA': 100.0})
    monkeypatch.setattr(po, 'load_previous_statuses', lambda c, d: {})
    out = po.run_ownership_pass(_Conn(cur), date(2026, 9, 15),
                                account_qty={'AAA': 100.0, 'ZZZ': 500.0},
                                log_fn=lambda m: None)
    assert out['rows'] == 2 and out['unallocated'] == 1 and out['shortfall'] == 0
    assert len(out['transitions']) == 1


def test_run_ownership_pass_swallows_a_db_failure(monkeypatch):
    def _boom(c):
        raise RuntimeError('relation position_ownership does not exist')
    monkeypatch.setattr(po, 'load_signal_qty', _boom)
    out = po.run_ownership_pass(_Conn(_Cursor()), date(2026, 9, 15),
                                account_qty={'AAA': 1.0}, log_fn=lambda m: None)
    assert 'skipped' in out


def test_run_ownership_pass_rolls_back_to_its_own_savepoint_on_failure(monkeypatch):
    # An UNAPPLIED migration 156 (table missing) must not leave the connection
    # InFailedSqlTransaction for whatever the reconcile step runs after this
    # pass — same SAVEPOINT / ROLLBACK TO contract as sp_broker_fills.
    cur = _Cursor()

    def _boom(c):
        raise RuntimeError('relation "position_ownership" does not exist')

    monkeypatch.setattr(po, 'load_signal_qty', _boom)
    conn = _Conn(cur)
    out = po.run_ownership_pass(conn, date(2026, 9, 15),
                                account_qty={'AAA': 1.0}, log_fn=lambda m: None)
    assert 'skipped' in out
    sqls = [sql for sql, _ in cur.calls]
    assert 'SAVEPOINT sp_ownership' in sqls
    assert 'ROLLBACK TO SAVEPOINT sp_ownership' in sqls
    # The savepoint's own release/rollback must never itself go through the
    # outer commit path — a failed pass commits nothing.
    assert conn.commits == 0
