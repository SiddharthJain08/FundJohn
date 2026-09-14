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


# ── collapse_fills / fetch_order_status carry the fill timestamp ────────────

def test_collapse_fills_carries_the_latest_transaction_time():
    out = ar.collapse_fills(_ACTIVITIES)
    assert out['o-entry']['filled_at'] == '2026-09-11T13:32:00Z'
    assert out['o-entry']['status'] == 'filled'
    assert abs(out['o-entry']['qty'] - 100.0) < 1e-9


def test_apply_fill_writes_filled_at_without_clobbering(monkeypatch):
    cur = _Cursor()
    ar._apply_fill(cur, 'sub-1', 'AAA',
                   {'status': 'filled', 'qty': 100.0, 'avg_price': 10.04,
                    'filled_at': '2026-09-11T13:32:00Z'}, dry_run=False)
    sql, params = cur.calls[0]
    assert 'filled_at=COALESCE(%s::timestamptz, filled_at)' in sql
    assert '2026-09-11T13:32:00Z' in params


def test_apply_fill_tolerates_a_record_without_a_timestamp():
    cur = _Cursor()
    ar._apply_fill(cur, 'sub-1', 'AAA',
                   {'status': 'partial', 'qty': 1.0, 'avg_price': 2.0}, dry_run=False)
    assert cur.calls[0][1][3] is None


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
    optional trap that raises on the first broker_fills INSERT — standing in
    for psycopg2.errors.UndefinedTable when migration 155 hasn't landed."""

    def __init__(self, submission_rows, raise_on_broker_fills_insert=None):
        self.calls = []
        self._rows = submission_rows
        self._raise = raise_on_broker_fills_insert

    def execute(self, sql, params=None):
        norm = ' '.join(sql.split())
        if self._raise is not None and norm.startswith('INSERT INTO broker_fills'):
            exc = self._raise
            self._raise = None  # raise once, like a real missing-table error
            raise exc
        self.calls.append((norm, params))

    def fetchall(self):
        return self._rows

    def close(self):
        pass

    def inserts(self):
        return [c for c in self.calls if c[0].startswith('INSERT INTO broker_fills')]

    def updates(self):
        return [c for c in self.calls if c[0].startswith('UPDATE')]


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
    monkeypatch.setattr(sr, 'fetch_recent_closed_orders',
                        lambda symbols, **kwargs: (True, []))
    monkeypatch.setattr(ar, 'fetch_fills_for_date', lambda *a, **k: _RECONCILE_FILLS)

    cur = _ReconcileCursor(
        [('sub-9', 'o-9', 'ZZZ', 5.0)],
        raise_on_broker_fills_insert=psycopg2.errors.UndefinedTable(
            'relation "broker_fills" does not exist'),
    )
    conn = _ReconcileConn(cur)

    n = ar.reconcile('2026-09-14', conn)  # must not raise

    assert n == 1
    assert cur.inserts() == []  # the one INSERT attempt raised and was not recorded
    # the submission UPDATE from earlier in reconcile() must survive the
    # savepoint rollback — the missing table can't poison the critical path.
    assert len(cur.updates()) == 1
    assert conn.commits == 1
