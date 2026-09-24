"""B2 ingest: every Alpaca FILL activity lands in broker_fills exactly once.

Alpaca activity records carry activity_id/order_id/symbol/side/qty/price/
transaction_time and NOTHING else — no parent_order_id, no client_order_id, no
order type or class. Those four come from a --nested closed-order read, walked
the same way classify_exit_fills walks legs.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import psycopg2.errors

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_reconcile as ar  # noqa: E402
from execution import stop_reattach as sr  # noqa: E402

MIG = ROOT / 'src' / 'database' / 'migrations' / '155_broker_fills.sql'


class _Cursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchall(self):
        return []

    def close(self):
        pass

    def inserts(self):
        return [c for c in self.calls if c[0].startswith('INSERT INTO broker_fills')]


_ACTIVITIES = [
    {'id': 'act-1', 'order_id': 'o-entry', 'symbol': 'AAA', 'side': 'buy',
     'qty': '60', 'price': '10.00', 'transaction_time': '2026-09-11T13:31:00Z',
     'order_status': 'partial_fill'},
    {'id': 'act-2', 'order_id': 'o-entry', 'symbol': 'AAA', 'side': 'buy',
     'qty': '40', 'price': '10.10', 'transaction_time': '2026-09-11T13:32:00Z',
     'order_status': 'filled'},
    {'id': 'act-3', 'order_id': 'o-stopleg', 'symbol': 'AAA', 'side': 'sell',
     'qty': '100', 'price': '9.40', 'transaction_time': '2026-09-11T19:31:00Z',
     'order_status': 'filled'},
]

_NESTED_ORDERS = [
    {'id': 'o-entry', 'symbol': 'AAA', 'client_order_id': 'oc_AAA_1',
     'type': 'market', 'order_class': 'bracket',
     'legs': [
         {'id': 'o-stopleg', 'symbol': 'AAA', 'client_order_id': 'oc_AAA_1_sl',
          'type': 'stop', 'stop_price': '9.50'},
         {'id': 'o-tpleg', 'symbol': 'AAA', 'client_order_id': 'oc_AAA_1_tp',
          'type': 'limit', 'limit_price': '12.00'},
     ]},
]


# ── _parse_ts ────────────────────────────────────────────────────────────

def test_parse_ts_passes_through_a_valid_iso8601_string():
    assert ar._parse_ts('2026-09-11T13:32:00Z') == '2026-09-11T13:32:00Z'


def test_parse_ts_rejects_missing_or_junk_values():
    assert ar._parse_ts(None) is None
    assert ar._parse_ts('') is None
    assert ar._parse_ts('not-a-timestamp') is None
    assert ar._parse_ts(12345) is None


# ── collapse_fills / fetch_order_status carry the fill timestamp ────────────

def test_collapse_fills_carries_the_latest_transaction_time():
    out = ar.collapse_fills(_ACTIVITIES)
    assert out['o-entry']['filled_at'] == '2026-09-11T13:32:00Z'
    assert out['o-entry']['status'] == 'filled'
    assert abs(out['o-entry']['qty'] - 100.0) < 1e-9


def test_apply_fill_first_update_is_byte_identical_to_pre_b2_shape():
    """The critical-path UPDATE (broker_status/filled_qty/filled_avg_price)
    must be untouched by B2 — migration 155 can land on a LATER johnbot
    restart than this code merges, so this statement can never depend on a
    column that might not exist yet."""
    cur = _Cursor()
    ar._apply_fill(cur, 'sub-1', 'AAA',
                   {'status': 'filled', 'qty': 100.0, 'avg_price': 10.04,
                    'filled_at': '2026-09-11T13:32:00Z'}, dry_run=False)
    sql, params = cur.calls[0]
    assert 'filled_at' not in sql
    assert params == ('filled', 100.0, 10.04, 'sub-1')


def test_apply_fill_writes_filled_at_in_its_own_savepoint():
    cur = _Cursor()
    ar._apply_fill(cur, 'sub-1', 'AAA',
                   {'status': 'filled', 'qty': 100.0, 'avg_price': 10.04,
                    'filled_at': '2026-09-11T13:32:00Z'}, dry_run=False)
    norms = [c[0] for c in cur.calls]
    assert 'SAVEPOINT sp_filled_at' in norms
    assert 'RELEASE SAVEPOINT sp_filled_at' in norms
    fa_calls = [c for c in cur.calls if 'SET filled_at' in c[0]]
    assert len(fa_calls) == 1
    sql, params = fa_calls[0]
    assert 'COALESCE(filled_at' in sql
    assert params == ('2026-09-11T13:32:00Z', 'sub-1')


def test_apply_fill_tolerates_a_record_without_a_timestamp():
    """No usable filled_at data -> the second UPDATE/savepoint is skipped
    entirely, not attempted-and-NULL."""
    cur = _Cursor()
    ar._apply_fill(cur, 'sub-1', 'AAA',
                   {'status': 'partial', 'qty': 1.0, 'avg_price': 2.0}, dry_run=False)
    assert len(cur.calls) == 1
    assert cur.calls[0][1] == ('partial', 1.0, 2.0, 'sub-1')


def test_apply_fill_skips_the_savepoint_for_an_unparseable_timestamp():
    cur = _Cursor()
    ar._apply_fill(cur, 'sub-1', 'AAA',
                   {'status': 'filled', 'qty': 1.0, 'avg_price': 2.0,
                    'filled_at': 'not-a-timestamp'}, dry_run=False)
    assert len(cur.calls) == 1


def test_apply_fill_survives_a_missing_filled_at_column(monkeypatch, capsys):
    """UndefinedColumn on the separate filled_at UPDATE (migration 155 not
    applied yet) must not propagate: the critical-path UPDATE already
    landed, the savepoint absorbs the failure, and exactly one warning is
    logged."""
    ar._reset_filled_at_missing_log()

    class _Boom(_Cursor):
        def execute(self, sql, params=None):
            norm = ' '.join(sql.split())
            if norm.startswith('UPDATE alpaca_submissions') and 'SET filled_at' in norm:
                super().execute(sql, params)
                raise psycopg2.errors.UndefinedColumn(
                    'column "filled_at" of relation "alpaca_submissions" does not exist')
            super().execute(sql, params)

    cur = _Boom()
    ar._apply_fill(cur, 'sub-1', 'AAA',
                   {'status': 'filled', 'qty': 100.0, 'avg_price': 10.04,
                    'filled_at': '2026-09-11T13:32:00Z'}, dry_run=False)  # must not raise

    status_calls = [c for c in cur.calls if 'SET broker_status' in c[0]]
    assert len(status_calls) == 1  # the critical-path UPDATE still landed
    assert 'ROLLBACK TO SAVEPOINT sp_filled_at' in [c[0] for c in cur.calls]
    out = capsys.readouterr().out
    assert out.count('filled_at column missing') == 1


# ── build_order_meta ───────────────────────────────────────────────────────

def test_build_order_meta_assigns_leg_parents():
    meta = ar.build_order_meta(_NESTED_ORDERS)
    assert meta['o-stopleg']['parent_order_id'] == 'o-entry'
    assert meta['o-stopleg']['order_type'] == 'stop'
    assert meta['o-stopleg']['client_order_id'] == 'oc_AAA_1_sl'
    assert meta['o-stopleg']['order_class'] == 'bracket'


def test_build_order_meta_top_level_order_has_no_parent():
    meta = ar.build_order_meta(_NESTED_ORDERS)
    assert meta['o-entry']['parent_order_id'] is None
    assert meta['o-entry']['order_type'] == 'market'


def test_build_order_meta_ignores_garbage_rows():
    assert ar.build_order_meta([None, 'nope', {'no_id': 1}]) == {}


# ── ingest_broker_fills ────────────────────────────────────────────────────

def test_ingest_writes_one_row_per_activity_with_on_conflict_do_nothing():
    cur = _Cursor()
    n = ar.ingest_broker_fills(cur, _ACTIVITIES, ar.build_order_meta(_NESTED_ORDERS))
    assert n == 3 and len(cur.inserts()) == 3
    sql, params = cur.inserts()[2]
    assert 'ON CONFLICT (activity_id) DO NOTHING' in sql
    assert params[0] == 'act-3'
    assert params[1] == 'o-stopleg'
    assert params[2] == 'o-entry'          # parent_order_id from the nested walk
    assert params[4] == 'AAA' and params[5] == 'sell'
    assert params[6] == 'stop'
    assert abs(params[8] - 100.0) < 1e-9 and abs(params[9] - 9.40) < 1e-9
    assert params[10] == '2026-09-11T19:31:00Z'


def test_ingest_leaves_order_shape_null_when_meta_is_missing():
    cur = _Cursor()
    ar.ingest_broker_fills(cur, _ACTIVITIES, {})
    assert cur.inserts()[0][1][2] is None
    assert cur.inserts()[0][1][6] is None


def test_ingest_skips_rows_without_an_activity_id():
    cur = _Cursor()
    assert ar.ingest_broker_fills(cur, [{'order_id': 'x', 'qty': '1', 'price': '1'}], {}) == 0
    assert cur.inserts() == []


def test_ingest_skips_rows_with_unparseable_numbers():
    cur = _Cursor()
    assert ar.ingest_broker_fills(cur, [{'id': 'a', 'qty': 'NaNsense', 'price': None}], {}) == 0


def test_ingest_dry_run_writes_nothing():
    cur = _Cursor()
    assert ar.ingest_broker_fills(cur, _ACTIVITIES, {}, dry_run=True) == 3
    assert cur.inserts() == []


def test_ingest_column_list_matches_migration_155():
    body = MIG.read_text()
    body = body[body.index('CREATE TABLE IF NOT EXISTS broker_fills'):]
    body = body[:body.index(');')]
    for col in ar._BROKER_FILL_COLUMNS:
        assert re.search(rf'^\s*{col}\s+\w', body, re.M), f'{col} not in migration 155'
    assert 'ingested_at' not in ar._BROKER_FILL_COLUMNS, 'ingested_at is a DB default'


# ── reconcile() wiring: the SAVEPOINT-isolated broker_fills append ─────────
#
# These exercise reconcile() itself (not just the helpers above) so the two
# controller-named edge cases — a failed closed-order read, and a missing
# broker_fills table — are proven against the real call path, not just
# asserted in prose. `fetch_recent_closed_orders` is imported INSIDE
# reconcile() (`from execution.stop_reattach import fetch_recent_closed_orders`),
# so it must be monkeypatched on the `stop_reattach` module object itself —
# patching it on `alpaca_reconcile` would silently no-op and, since our fill
# fixture below carries `symbol`, would shell out to the real Alpaca CLI.

class _ReconcileCursor:
    """Fake cursor with a scripted fetchall() (submissions rows) and an
    honesty contract a stub-that-just-records can't fake its way past: after
    `trigger(norm_sql)` matches once and raises `raise_exc`, EVERY subsequent
    execute() raises psycopg2.errors.InFailedSqlTransaction until a
    'ROLLBACK TO SAVEPOINT' statement clears it — the same thing a real
    aborted Postgres transaction enforces. Without this, a test could pass
    even if production code forgot the ROLLBACK TO SAVEPOINT line entirely
    (the trap fires once, everything after is silently accepted)."""

    def __init__(self, submission_rows, trigger=None, raise_exc=None):
        self.calls = []
        self._rows = submission_rows
        self._trigger = trigger
        self._raise_exc = raise_exc
        self._aborted = False

    def execute(self, sql, params=None):
        norm = ' '.join(sql.split())
        if norm.startswith('ROLLBACK TO SAVEPOINT'):
            self._aborted = False
            self.calls.append((norm, params))
            return
        if self._aborted:
            raise psycopg2.errors.InFailedSqlTransaction(
                'current transaction is aborted, commands ignored until end of transaction block')
        if self._trigger is not None and self._trigger(norm):
            self._trigger = None  # raise once, like a real missing-table/-column error
            self._aborted = True
            raise self._raise_exc
        self.calls.append((norm, params))

    def fetchall(self):
        return self._rows

    def close(self):
        pass

    def inserts(self):
        return [c for c in self.calls if c[0].startswith('INSERT INTO broker_fills')]

    def status_updates(self):
        return [c for c in self.calls if 'SET broker_status' in c[0]]

    def filled_at_updates(self):
        return [c for c in self.calls if 'SET filled_at' in c[0]]


class _ReconcileConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass


_RECONCILE_FILLS = [
    {'id': 'act-9', 'order_id': 'o-9', 'symbol': 'ZZZ', 'side': 'buy',
     'qty': '5', 'price': '20.00', 'order_status': 'filled',
     'transaction_time': '2026-09-14T13:30:00Z'},
]


def test_reconcile_ingests_fills_even_when_closed_order_read_fails(monkeypatch):
    calls = []

    def _fake_fetch_recent_closed_orders(symbols, **kwargs):
        calls.append((tuple(symbols), kwargs))
        return False, []  # every closed-order read failed → no enrichment

    monkeypatch.setattr(sr, 'fetch_recent_closed_orders', _fake_fetch_recent_closed_orders)
    monkeypatch.setattr(ar, 'fetch_fills_for_date', lambda *a, **k: _RECONCILE_FILLS)

    cur = _ReconcileCursor([('sub-9', 'o-9', 'ZZZ', 5.0)])
    conn = _ReconcileConn(cur)

    n = ar.reconcile('2026-09-14', conn)

    assert n == 1
    assert calls, 'fetch_recent_closed_orders must be called (and thus monkeypatched, not the real CLI)'
    assert calls[0][0] == ('ZZZ',)
    ins = cur.inserts()
    assert len(ins) == 1
    _, params = ins[0]
    assert params[0] == 'act-9'
    assert params[2] is None and params[6] is None and params[7] is None  # parent/type/class NULL
    assert conn.commits == 1


def test_reconcile_survives_a_missing_broker_fills_table(monkeypatch):
    """UndefinedTable on the broker_fills INSERT (migration 155 not applied
    yet) must be caught, logged once, and reconcile() must proceed to
    commit — proven with the aborted-flag fake, not just a bare `except
    Exception` that happens not to be exercised."""
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders',
                        lambda symbols, **kwargs: (True, []))
    monkeypatch.setattr(ar, 'fetch_fills_for_date', lambda *a, **k: _RECONCILE_FILLS)

    cur = _ReconcileCursor(
        [('sub-9', 'o-9', 'ZZZ', 5.0)],
        trigger=lambda norm: norm.startswith('INSERT INTO broker_fills'),
        raise_exc=psycopg2.errors.UndefinedTable('relation "broker_fills" does not exist'),
    )
    conn = _ReconcileConn(cur)

    n = ar.reconcile('2026-09-14', conn)  # must not raise

    assert n == 1
    assert cur.inserts() == []  # the one INSERT attempt raised and was not recorded
    # the submission UPDATE from earlier in reconcile() survived — the
    # missing table can't poison the critical path — and the aborted-flag
    # fake proves this: it would have raised InFailedSqlTransaction on
    # RELEASE SAVEPOINT (and on conn.commit()'s implicit end-of-statement)
    # had the ROLLBACK TO SAVEPOINT line been skipped.
    assert len(cur.status_updates()) == 1
    assert 'ROLLBACK TO SAVEPOINT sp_broker_fills' in [c[0] for c in cur.calls]
    assert conn.commits == 1


def test_reconcile_survives_a_missing_filled_at_column(monkeypatch, capsys):
    """UndefinedColumn on the separate filled_at UPDATE (migration 155 not
    applied yet) must not touch the sibling sp_broker_fills savepoint: the
    submission's critical-path UPDATE lands, the broker_fills INSERT for the
    same cycle still lands, and reconcile() commits."""
    ar._reset_filled_at_missing_log()
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders',
                        lambda symbols, **kwargs: (True, []))
    monkeypatch.setattr(ar, 'fetch_fills_for_date', lambda *a, **k: _RECONCILE_FILLS)

    cur = _ReconcileCursor(
        [('sub-9', 'o-9', 'ZZZ', 5.0)],
        trigger=lambda norm: norm.startswith('UPDATE alpaca_submissions') and 'SET filled_at' in norm,
        raise_exc=psycopg2.errors.UndefinedColumn(
            'column "filled_at" of relation "alpaca_submissions" does not exist'),
    )
    conn = _ReconcileConn(cur)

    n = ar.reconcile('2026-09-14', conn)  # must not raise

    assert n == 1
    assert len(cur.status_updates()) == 1   # the critical-path UPDATE still landed
    assert cur.filled_at_updates() == []    # the attempted write never committed
    assert 'ROLLBACK TO SAVEPOINT sp_filled_at' in [c[0] for c in cur.calls]
    assert len(cur.inserts()) == 1          # broker_fills ingest still ran — unrelated savepoint
    assert conn.commits == 1
    out = capsys.readouterr().out
    assert out.count('filled_at column missing') == 1
