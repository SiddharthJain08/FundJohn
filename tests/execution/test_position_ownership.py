"""B3 (spec item 15): do the shares the broker holds match what signals claim?

unknown = account_qty - signal_qty on SIGNED quantities, so a short book reads
the same way a long one does. Tolerances are asymmetric — a shortfall (signals
marking a position the broker doesn't hold) is the dangerous direction — and
floored at 1 share so rounding dust is never a finding.

Fix round 1 (2026-09-14) added: failure isolation for the cursor/SAVEPOINT/
commit path (item 1), DO UPDATE upsert semantics (item 2), crypto exclusion
(item 3), per-submission dedupe in load_signal_qty (item 4), and the unknown/
NULL-direction skip+log + dry_run + str-cycle_date minors (item 6).
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
    """Single-answer fake cursor: every execute() call's fetchall() returns the
    same `rows` regardless of statement — fine for the tests below that only
    ever issue one SELECT, or that don't care about fetchall() at all. Can be
    made to raise on a specific statement via `raise_on(sql) -> bool`."""

    def __init__(self, rows=(), raise_on=None):
        self.rows = list(rows)
        self.calls = []
        self.raise_on = raise_on
        self.rowcount = 1
        self.closed = False

    def execute(self, sql, params=None):
        norm = ' '.join(sql.split())
        self.calls.append((norm, params))
        if self.raise_on and self.raise_on(norm):
            raise RuntimeError(f'boom: {norm}')

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ScriptedCursor:
    """Fake cursor whose fetchall() answer depends on the just-executed SQL,
    matched by substring in the order given — needed once a loader issues more
    than one distinct query (load_signal_qty: matched entries, then a
    per-ticker exit-net query)."""

    def __init__(self, answers):
        self.answers = list(answers)  # [(substring, rows), ...]
        self.calls = []
        self._last_rows = []
        self.closed = False

    def execute(self, sql, params=None):
        norm = ' '.join(sql.split())
        self.calls.append((norm, params))
        for substr, rows in self.answers:
            if substr in norm:
                self._last_rows = rows
                return
        self._last_rows = []

    def fetchall(self):
        return self._last_rows

    def close(self):
        self.closed = True


class _Conn:
    def __init__(self, cur, raise_on_commit=False):
        self._cur = cur
        self.commits = 0
        self.raise_on_commit = raise_on_commit

    def cursor(self):
        return self._cur

    def commit(self):
        if self.raise_on_commit:
            raise RuntimeError('commit failed')
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


# ── load_account_qty ───────────────────────────────────────────────────────

def test_load_account_qty_sums_signed_share_counts():
    positions = [{'symbol': 'AAA', 'qty': '100'}, {'symbol': 'BBB', 'qty': '-50'}]
    assert po.load_account_qty(lambda: positions) == {'AAA': 100.0, 'BBB': -50.0}


def test_load_account_qty_returns_none_when_the_broker_is_unreadable():
    assert po.load_account_qty(lambda: None) is None


# ── crypto exclusion (fix round 1, item 3) ────────────────────────────────

def test_is_crypto_ticker_matches_the_base_usd_convention():
    assert po._is_crypto_ticker('BTC-USD') is True
    assert po._is_crypto_ticker('AAA') is False


def test_exclude_crypto_strips_base_usd_tickers():
    filtered, crypto = po._exclude_crypto({'AAA': 100.0, 'BTC-USD': 5.0})
    assert filtered == {'AAA': 100.0}
    assert crypto == {'BTC-USD'}


def test_exclude_crypto_is_a_no_op_when_nothing_matches():
    filtered, crypto = po._exclude_crypto({'AAA': 100.0})
    assert filtered == {'AAA': 100.0}
    assert crypto == set()


def test_run_ownership_pass_excludes_a_crypto_signal_and_counts_it(monkeypatch):
    monkeypatch.setattr(po, 'load_signal_qty', lambda c, **k: {'AAA': 100.0, 'BTC-USD': 5.0})
    monkeypatch.setattr(po, 'load_previous_statuses', lambda c, d: {})
    out = po.run_ownership_pass(_Conn(_Cursor()), date(2026, 9, 15),
                                account_qty={'AAA': 100.0}, log_fn=lambda m: None)
    # BTC-USD never produces a ledger row (not unallocated, not shortfall — absent).
    assert out['rows'] == 1
    assert out['crypto_skipped'] == 1


def test_run_ownership_pass_swallows_a_crypto_helper_import_failure(monkeypatch):
    # _exclude_crypto(account_qty) runs BEFORE the cursor/SAVEPOINT try even
    # opens (it needs no DB connection) — its lazy `from execution.
    # alpaca_executor import _is_crypto_ticker` failing (e.g. a broken import
    # chain) must land in the same {'skipped': …} path as a DB failure, not
    # raise past run_ownership_pass (fix round 1, item 1 applied to the
    # item-3 crypto path).
    def _boom(t):
        raise ImportError("No module named 'execution.alpaca_executor'")
    monkeypatch.setattr(po, '_is_crypto_ticker', _boom)
    out = po.run_ownership_pass(_Conn(_Cursor()), date(2026, 9, 15),
                                account_qty={'AAA': 1.0}, log_fn=lambda m: None)
    assert 'skipped' in out


# ── load_signal_qty: dedupe (item 4) + unknown-direction skip (item 6) ────

def test_matched_entry_sql_has_distinct_on_dedupe():
    sql = ' '.join(po._MATCHED_ENTRY_SQL.split())
    assert 'DISTINCT ON (s.run_date, s.strategy_id, s.ticker)' in sql
    assert sql.strip().endswith('ORDER BY s.run_date, s.strategy_id, s.ticker, es.id')


def test_load_signal_qty_counts_a_shared_submission_once():
    # Simulates the real DISTINCT ON failing to collapse two execution_signals
    # rows (different signal_date) that share ONE physical submission — the
    # Python seen-set must still count that submission's filled_qty exactly
    # once, not twice.
    cur = _ScriptedCursor([
        ('FROM execution_signals', [
            ('2026-09-15', 's1', 'AAA', 'LONG', 100.0, 't1'),
            ('2026-09-15', 's1', 'AAA', 'LONG', 100.0, 't1'),
        ]),
        ('FROM broker_fills', [(0.0,)]),
    ])
    assert po.load_signal_qty(cur, log_fn=lambda m: None) == {'AAA': 100.0}


def test_load_signal_qty_nets_exit_fills_since_the_earliest_entry():
    cur = _ScriptedCursor([
        ('FROM execution_signals', [('2026-09-15', 's1', 'AAA', 'LONG', 100.0, 't1')]),
        ('FROM broker_fills', [(-40.0,)]),
    ])
    assert po.load_signal_qty(cur, log_fn=lambda m: None) == {'AAA': 60.0}


def test_load_signal_qty_skips_and_logs_an_unknown_direction():
    logged = []
    cur = _ScriptedCursor([
        ('FROM execution_signals', [
            ('2026-09-15', 's1', 'AAA', 'WEIRD', 100.0, 't1'),
            ('2026-09-15', 's2', 'BBB', 'LONG', 50.0, 't2'),
        ]),
        ('FROM broker_fills', [(0.0,)]),
    ])
    out = po.load_signal_qty(cur, log_fn=logged.append)
    assert 'AAA' not in out
    assert out['BBB'] == 50.0
    assert any('direction' in m.lower() for m in logged)


def test_load_signal_qty_skips_a_null_direction():
    cur = _ScriptedCursor([
        ('FROM execution_signals', [('2026-09-15', 's1', 'AAA', None, 100.0, 't1')]),
        ('FROM broker_fills', [(0.0,)]),
    ])
    assert po.load_signal_qty(cur, log_fn=lambda m: None) == {}


def test_load_signal_qty_short_direction_is_negative():
    cur = _ScriptedCursor([
        ('FROM execution_signals', [('2026-09-15', 's1', 'AAA', 'SHORT', 100.0, 't1')]),
        ('FROM broker_fills', [(0.0,)]),
    ])
    assert po.load_signal_qty(cur, log_fn=lambda m: None) == {'AAA': -100.0}


# ── persist_ownership: DO UPDATE, not append-only (fix round 1, item 2) ───

def test_persist_ownership_upserts_with_do_update_never_delete():
    cur = _Cursor()
    rows = po.compute_ownership({'AAA': 100.0}, {'AAA': 100.0})
    assert po.persist_ownership(cur, date(2026, 9, 15), rows) == 1
    sql, params = cur.calls[0]
    assert sql.startswith('INSERT INTO position_ownership')
    assert 'ON CONFLICT (cycle_date, ticker) DO UPDATE SET' in sql
    assert 'account_qty = EXCLUDED.account_qty' in sql
    assert 'signal_qty = EXCLUDED.signal_qty' in sql
    assert 'status = EXCLUDED.status' in sql
    assert 'DO NOTHING' not in sql
    assert 'DELETE' not in sql.upper() and 'TRUNCATE' not in sql.upper()
    assert params[1] == 'AAA' and params[5] == po.STATUS_OK


def test_persist_ownership_returns_the_cursors_rowcount_not_len_rows():
    class _ZeroRowcountCursor(_Cursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            self.rowcount = 0

    cur = _ZeroRowcountCursor()
    rows = po.compute_ownership({'AAA': 100.0, 'BBB': 200.0}, {})
    # rowcount pinned to 0 on every execute -> persist reports 0 written, even
    # though 2 rows were passed in.
    assert po.persist_ownership(cur, date(2026, 9, 15), rows) == 0
    assert len(rows) == 2


# ── run_ownership_pass: failure isolation (fix round 1, item 1, CRITICAL) ──

def test_run_ownership_pass_skips_when_the_broker_is_unreadable(monkeypatch):
    monkeypatch.setattr(po, 'load_account_qty', lambda *a, **k: None)
    out = po.run_ownership_pass(_Conn(_Cursor()), date(2026, 9, 15),
                                log_fn=lambda m: None)
    assert out == {'skipped': 'broker_unavailable'}


def test_run_ownership_pass_counts_and_never_raises(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(po, 'load_signal_qty', lambda c, **k: {'AAA': 100.0})
    monkeypatch.setattr(po, 'load_previous_statuses', lambda c, d: {})
    out = po.run_ownership_pass(_Conn(cur), date(2026, 9, 15),
                                account_qty={'AAA': 100.0, 'ZZZ': 500.0},
                                log_fn=lambda m: None)
    assert out['rows'] == 2 and out['unallocated'] == 1 and out['shortfall'] == 0
    assert len(out['transitions']) == 1
    assert out['crypto_skipped'] == 0


def test_run_ownership_pass_swallows_a_db_failure(monkeypatch):
    def _boom(c, **k):
        raise RuntimeError('relation position_ownership does not exist')
    monkeypatch.setattr(po, 'load_signal_qty', _boom)
    out = po.run_ownership_pass(_Conn(_Cursor()), date(2026, 9, 15),
                                account_qty={'AAA': 1.0}, log_fn=lambda m: None)
    assert 'skipped' in out


def test_run_ownership_pass_swallows_a_savepoint_failure_and_never_raises(monkeypatch):
    # The cursor open + SAVEPOINT now live INSIDE the try (item 1): a psycopg2
    # error raised by the SAVEPOINT statement itself must still come back as
    # {'skipped': …} rather than propagate — the discriminating case the old,
    # replaced "rolls back on failure" test didn't isolate (it only ever broke
    # load_signal_qty, downstream of the SAVEPOINT).
    cur = _Cursor(raise_on=lambda sql: sql == 'SAVEPOINT sp_ownership')
    conn = _Conn(cur)
    out = po.run_ownership_pass(conn, date(2026, 9, 15),
                                account_qty={'AAA': 1.0}, log_fn=lambda m: None)
    assert 'skipped' in out
    assert conn.commits == 0
    assert cur.closed is True
    sqls = [sql for sql, _ in cur.calls]
    assert 'SAVEPOINT sp_ownership' in sqls
    assert 'ROLLBACK TO SAVEPOINT sp_ownership' in sqls


def test_run_ownership_pass_swallows_a_commit_failure_and_never_raises(monkeypatch):
    # conn.commit() now lives inside the try (item 1): a failure there must
    # roll back to the savepoint and return {'skipped': …}, not raise past
    # run_ownership_pass into alpaca_reconcile.main() (which only catches
    # RuntimeError).
    cur = _Cursor()
    monkeypatch.setattr(po, 'load_signal_qty', lambda c, **k: {'AAA': 100.0})
    monkeypatch.setattr(po, 'load_previous_statuses', lambda c, d: {})
    conn = _Conn(cur, raise_on_commit=True)
    out = po.run_ownership_pass(conn, date(2026, 9, 15),
                                account_qty={'AAA': 100.0}, log_fn=lambda m: None)
    assert 'skipped' in out
    assert cur.closed is True
    sqls = [sql for sql, _ in cur.calls]
    assert 'ROLLBACK TO SAVEPOINT sp_ownership' in sqls


def test_run_ownership_pass_swallows_a_double_fault(monkeypatch):
    # The SAVEPOINT statement AND the recovery ROLLBACK TO / RELEASE both
    # raise (a thoroughly broken connection) — must still return {'skipped':
    # …}, never propagate.
    cur = _Cursor(raise_on=lambda sql: True)
    conn = _Conn(cur)
    out = po.run_ownership_pass(conn, date(2026, 9, 15),
                                account_qty={'AAA': 1.0}, log_fn=lambda m: None)
    assert 'skipped' in out
    assert cur.closed is True


def test_run_ownership_pass_closes_the_cursor_on_a_clean_run(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(po, 'load_signal_qty', lambda c, **k: {'AAA': 100.0})
    monkeypatch.setattr(po, 'load_previous_statuses', lambda c, d: {})
    po.run_ownership_pass(_Conn(cur), date(2026, 9, 15),
                          account_qty={'AAA': 100.0}, log_fn=lambda m: None)
    assert cur.closed is True


# ── minors (item 6): dry_run and a str cycle_date ─────────────────────────

def test_run_ownership_pass_dry_run_never_inserts_or_commits(monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(po, 'load_signal_qty', lambda c, **k: {'AAA': 100.0})
    monkeypatch.setattr(po, 'load_previous_statuses', lambda c, d: {})
    conn = _Conn(cur)
    out = po.run_ownership_pass(conn, date(2026, 9, 15), dry_run=True,
                                account_qty={'AAA': 100.0}, log_fn=lambda m: None)
    assert out['rows'] == 1
    assert conn.commits == 0
    assert all('INSERT INTO position_ownership' not in sql for sql, _ in cur.calls)


def test_run_ownership_pass_accepts_a_str_cycle_date_like_production(monkeypatch):
    # main() passes args.date, an argparse str, not a datetime.date.
    cur_str = _Cursor()
    cur_date = _Cursor()
    monkeypatch.setattr(po, 'load_signal_qty', lambda c, **k: {'AAA': 100.0})
    monkeypatch.setattr(po, 'load_previous_statuses', lambda c, d: {})
    out_str = po.run_ownership_pass(_Conn(cur_str), '2026-09-15',
                                    account_qty={'AAA': 100.0}, log_fn=lambda m: None)
    out_date = po.run_ownership_pass(_Conn(cur_date), date(2026, 9, 15),
                                     account_qty={'AAA': 100.0}, log_fn=lambda m: None)
    assert out_str['rows'] == out_date['rows']
    assert out_str['unallocated'] == out_date['unallocated']
    assert out_str['shortfall'] == out_date['shortfall']
    # The str actually reaches the INSERT's cycle_date param unchanged (not
    # just "no Python type error") — production passes args.date, an argparse
    # str, straight through to persist_ownership.
    insert_calls = [c for c in cur_str.calls if c[0].startswith('INSERT INTO position_ownership')]
    assert insert_calls and insert_calls[0][1][0] == '2026-09-15'
