"""tests/test_alpaca_reconcile.py

Unit tests for src/execution/alpaca_reconcile.py (Phase 1.3 of alpaca-cli
integration). Mocks the alpaca CLI subprocess and exercises the fill→update
logic against an in-memory psycopg2 fake — no live broker calls, no real
DB required.

Run:
    pytest tests/test_alpaca_reconcile.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_reconcile as ar  # noqa: E402


def _mock_proc(returncode=0, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class FakeCursor:
    """Minimal cursor that records executes and replays a queued result for fetchall."""
    def __init__(self, fetchall_rows=None):
        self._rows    = fetchall_rows or []
        self.calls    = []   # list of (sql, params)

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, fetchall_rows=None):
        self.cur = FakeCursor(fetchall_rows)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass


# ── fetch_order_status CLI contract ──────────────────────────────────────────

class TestFetchOrderStatusCli(unittest.TestCase):
    """The Alpaca CLI's `order get` requires `--order-id <id>`; a bare
    positional arg returns an error JSON (`{"code":0,"error":"--order-id
    required",...}`) with rc=0 and no `status` key, so fetch_order_status
    silently returned None for EVERY order. That dead fallback is why the
    poll-to-terminal pass never upgraded anything and 119 ext-hours fills
    accreted at broker_status=NULL since 2026-04-21. Assert the flag form."""

    def test_invokes_order_get_with_order_id_flag(self):
        filled = json.dumps({'id': 'ord-Z', 'status': 'filled',
                             'filled_qty': '5', 'filled_avg_price': '10.0'})
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, filled, '')) as mock_run:
            rec = ar.fetch_order_status('ord-Z')
        argv = mock_run.call_args[0][0]
        self.assertIn('--order-id', argv)
        self.assertEqual(argv[argv.index('--order-id') + 1], 'ord-Z')
        self.assertEqual(rec, {'qty': 5.0, 'avg_price': 10.0, 'status': 'filled'})


# ── collapse_fills tests ─────────────────────────────────────────────────────

class TestCollapseFills(unittest.TestCase):
    def test_single_complete_fill_marks_filled(self):
        fills = [{
            'order_id':         'ord-1',
            'qty':              '10',
            'price':            '100.50',
            'order_status':     'filled',
            'transaction_time': '2026-04-28T14:30:00Z',
        }]
        out = ar.collapse_fills(fills)
        self.assertEqual(out['ord-1']['qty'],        10.0)
        self.assertEqual(out['ord-1']['avg_price'], 100.50)
        self.assertEqual(out['ord-1']['status'],   'filled')

    def test_partial_fill_marks_partial(self):
        fills = [{
            'order_id': 'ord-2', 'qty': '3', 'price': '50.0',
            'order_status': 'partially_filled',
            'transaction_time': '2026-04-28T14:00:00Z',
        }]
        out = ar.collapse_fills(fills)
        self.assertEqual(out['ord-2']['status'], 'partial')
        self.assertEqual(out['ord-2']['qty'], 3.0)

    def test_multiple_partials_aggregate_qty_weighted_avg(self):
        # 3 shares @ 100, then 2 @ 110 → cum_qty=5, avg=104.0; final fully filled
        fills = [
            {'order_id': 'ord-3', 'qty': '3', 'price': '100',
             'order_status': 'partially_filled',
             'transaction_time': '2026-04-28T14:00:00Z'},
            {'order_id': 'ord-3', 'qty': '2', 'price': '110',
             'order_status': 'filled',
             'transaction_time': '2026-04-28T14:05:00Z'},
        ]
        out = ar.collapse_fills(fills)
        self.assertEqual(out['ord-3']['qty'], 5.0)
        self.assertAlmostEqual(out['ord-3']['avg_price'], 104.0, places=4)
        # latest timestamp had order_status=filled → status=filled
        self.assertEqual(out['ord-3']['status'], 'filled')

    def test_multiple_orders_split(self):
        fills = [
            {'order_id': 'ord-a', 'qty': '1', 'price': '10',
             'order_status': 'filled', 'transaction_time': 't1'},
            {'order_id': 'ord-b', 'qty': '2', 'price': '20',
             'order_status': 'filled', 'transaction_time': 't1'},
        ]
        out = ar.collapse_fills(fills)
        self.assertEqual(set(out.keys()), {'ord-a', 'ord-b'})

    def test_skips_rows_without_order_id_or_invalid_qty(self):
        fills = [
            {'order_id': '',         'qty': '1', 'price': '10', 'order_status': 'filled'},
            {'order_id': 'ord-c',    'qty': '?', 'price': '10', 'order_status': 'filled'},
            {'order_id': 'ord-d',    'qty': '2', 'price': '20', 'order_status': 'filled'},
        ]
        out = ar.collapse_fills(fills)
        # ord-c was skipped due to non-numeric qty; ord-d is the only valid one
        self.assertEqual(set(out.keys()), {'ord-d'})


# ── reconcile() integration with mocked DB + CLI ─────────────────────────────

class TestReconcile(unittest.TestCase):
    def test_fill_updates_submission_to_filled(self):
        conn = FakeConn(fetchall_rows=[
            ('sub-uuid-1', 'broker-order-1', 'AAPL', 10),
        ])
        fill_json = json.dumps([
            {'order_id': 'broker-order-1', 'qty': '10', 'price': '150.00',
             'order_status': 'filled',
             'transaction_time': '2026-04-28T14:30:00Z'},
        ])
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, fill_json, '')) as mock_run:
            ar.reconcile('2026-04-28', conn)

        # 2 executes: SELECT submissions, UPDATE on the matched row
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 1)
        sql, params = update_calls[0]
        self.assertEqual(params[0], 'filled')
        self.assertEqual(params[1], 10.0)
        self.assertEqual(params[2], 150.00)
        self.assertEqual(params[3], 'sub-uuid-1')
        # CLI was invoked with correct args
        argv = mock_run.call_args[0][0]
        self.assertIn('--activity-types', argv)
        self.assertEqual(argv[argv.index('--activity-types') + 1], 'FILL')
        self.assertIn('--date', argv)
        self.assertEqual(argv[argv.index('--date') + 1], '2026-04-28')

    def test_partial_fill_marked_partial(self):
        conn = FakeConn(fetchall_rows=[
            ('sub-uuid-2', 'broker-order-2', 'MSFT', 10),
        ])
        # Only 4 of 10 shares filled
        fill_json = json.dumps([
            {'order_id': 'broker-order-2', 'qty': '4', 'price': '400.0',
             'order_status': 'partially_filled',
             'transaction_time': '2026-04-28T14:30:00Z'},
        ])
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, fill_json, '')):
            ar.reconcile('2026-04-28', conn)
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(update_calls[0][1][0], 'partial')
        self.assertEqual(update_calls[0][1][1], 4.0)

    def test_unmatched_submission_left_as_submitted_when_ambiguous(self):
        """Post-ca3a589 contract: when no FILL activity is found AND the
        order-status fallback is unfetchable (CLI failure, non-dict
        response, still in-flight), the row is LEFT AS submitted rather
        than aggressively marked rejected_by_broker.

        Rationale: 2026-05-21 reconcile-lag incident showed that running
        ~1s after order submission, only 32/52 FILL activities had
        propagated. Two market-orders (APA, INTC) had no fill activity
        and were wrongly flagged rejected, even though they were fully
        filled at the broker. The new behavior gives ambiguous evidence
        the benefit of the doubt — a subsequent reconcile pass (or the
        operator's manual sweep) can re-evaluate once the broker has
        caught up.
        """
        conn = FakeConn(fetchall_rows=[
            ('sub-uuid-3', 'broker-order-rejected', 'TSLA', 5),
        ])
        # Mock returns `[]` for BOTH calls (activity fetch + order-get fallback).
        # fetch_order_status sees a list (non-dict), returns None → leave-as-submitted.
        # poll_timeout_s=0 disables the new poll pass so this test stays
        # focused on the first-pass ambiguity contract.
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, '[]', '')):
            ar.reconcile('2026-04-28', conn, poll_timeout_s=0)
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 0,
                         'ambiguous unmatched submissions must not be updated; '
                         'the row stays at status=submitted for the next reconcile pass')

    def test_unmatched_submission_marked_rejected_when_broker_confirms(self):
        """When the order-status fallback positively confirms a terminal
        rejected/canceled/expired state, the row IS marked rejected_by_broker.
        Distinct from the ambiguous case above: here we have evidence."""
        conn = FakeConn(fetchall_rows=[
            ('sub-uuid-3', 'broker-order-rejected', 'TSLA', 5),
        ])
        rejected_order_json = json.dumps({
            'id': 'broker-order-rejected',
            'status': 'rejected',
            'filled_qty': '0',
            'filled_avg_price': '0',
        })
        # 1st subprocess call: account activities → empty list.
        # 2nd subprocess call: order get → rejected-status object.
        responses = [_mock_proc(0, '[]', ''),
                     _mock_proc(0, rejected_order_json, '')]
        with patch('execution.alpaca_reconcile.subprocess.run',
                   side_effect=responses):
            ar.reconcile('2026-04-28', conn)
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 1)
        self.assertIn('rejected_by_broker', update_calls[0][0])

    def test_no_submissions_today_exits_clean(self):
        conn = FakeConn(fetchall_rows=[])
        with patch('execution.alpaca_reconcile.subprocess.run') as mock_run:
            count = ar.reconcile('2026-04-28', conn)
        self.assertEqual(count, 0)
        mock_run.assert_not_called()
        self.assertEqual(conn.commits, 0)

    def test_cli_error_raises_runtime_error(self):
        conn = FakeConn(fetchall_rows=[
            ('sub-uuid-4', 'broker-order-4', 'AAPL', 1),
        ])
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(1, '', 'authentication required')):
            with self.assertRaises(RuntimeError):
                ar.reconcile('2026-04-28', conn)


class TestReconcilePolling(unittest.TestCase):
    """Fix 3 (2026-05-21): ext-hours fills routinely lag the activities
    API by 5-30s. After the first pass leaves an order as in-flight, the
    reconciler polls fetch_order_status every `poll_interval_s` until
    terminal or timeout. Without this, broker_status stays NULL until the
    NEXT cycle runs reconcile."""

    def test_in_flight_then_filled_after_poll(self):
        """First fetch_order_status returns None (in-flight); second returns
        filled. Reconciler must upgrade the row to filled."""
        conn = FakeConn(fetchall_rows=[
            ('sub-uuid-W', 'broker-order-W', 'WBD', 1),
        ])
        # 1) activity list: empty
        # 2) order get (first pass): still pending → CLI returns a non-dict shape so
        #    fetch_order_status returns None → row goes to in_flight bucket
        # 3) order get (poll pass): filled
        filled_resp = json.dumps({
            'id': 'broker-order-W', 'status': 'filled',
            'filled_qty': '1', 'filled_avg_price': '27.15',
        })
        responses = [
            _mock_proc(0, '[]', ''),          # activities
            _mock_proc(0, '[]', ''),           # 1st order get → non-dict → None
            _mock_proc(0, filled_resp, ''),    # poll → filled
        ]
        with patch('execution.alpaca_reconcile.subprocess.run',
                   side_effect=responses), \
             patch('execution.alpaca_reconcile.time.sleep'):
            ar.reconcile('2026-05-21', conn,
                         poll_timeout_s=10, poll_interval_s=1)

        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 1,
                         'poll-detected fill must produce an UPDATE')
        sql, params = update_calls[0]
        self.assertEqual(params[0], 'filled')
        self.assertEqual(params[1], 1.0)
        self.assertEqual(params[2], 27.15)
        self.assertEqual(params[3], 'sub-uuid-W')

    def test_in_flight_throughout_poll_stays_submitted(self):
        """If the order stays in-flight for the entire poll window, the
        row is left as submitted (legacy fail-open behavior)."""
        conn = FakeConn(fetchall_rows=[
            ('sub-uuid-X', 'broker-order-X', 'XYZ', 1),
        ])
        # All subprocess calls return `[]` → non-dict → None throughout.
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, '[]', '')), \
             patch('execution.alpaca_reconcile.time.sleep'):
            ar.reconcile('2026-05-21', conn,
                         poll_timeout_s=5, poll_interval_s=1)
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 0,
                         'still-in-flight rows must be left as submitted')

    def test_in_flight_then_rejected_after_poll(self):
        """Order becomes terminal-rejected during the poll window → row
        marked rejected_by_broker, no spurious "filled" write."""
        conn = FakeConn(fetchall_rows=[
            ('sub-uuid-R', 'broker-order-R', 'BAD', 1),
        ])
        rejected_resp = json.dumps({
            'id': 'broker-order-R', 'status': 'rejected',
            'filled_qty': '0', 'filled_avg_price': '0',
        })
        responses = [
            _mock_proc(0, '[]', ''),          # activities
            _mock_proc(0, '[]', ''),           # 1st order get → None
            _mock_proc(0, rejected_resp, ''),  # poll → rejected
        ]
        with patch('execution.alpaca_reconcile.subprocess.run',
                   side_effect=responses), \
             patch('execution.alpaca_reconcile.time.sleep'):
            ar.reconcile('2026-05-21', conn,
                         poll_timeout_s=10, poll_interval_s=1)
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 1)
        sql, _ = update_calls[0]
        self.assertIn('rejected_by_broker', sql)

    def test_poll_disabled_with_zero_timeout(self):
        """poll_timeout_s=0 preserves legacy "no poll" behavior — in-flight
        orders are immediately left as submitted with no time.sleep calls."""
        conn = FakeConn(fetchall_rows=[
            ('sub-uuid-Y', 'broker-order-Y', 'YYY', 1),
        ])
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, '[]', '')), \
             patch('execution.alpaca_reconcile.time.sleep') as mock_sleep:
            ar.reconcile('2026-05-21', conn, poll_timeout_s=0)
        mock_sleep.assert_not_called()
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 0)


if __name__ == '__main__':
    unittest.main()
