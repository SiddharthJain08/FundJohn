"""tests/test_alpaca_reconcile_sweep.py

Unit tests for the stale-NULL re-sweep mode in
src/execution/alpaca_reconcile.py (sweep_stale / --sweep-stale).

Background: the normal reconcile() only touches the CURRENT cycle's
run_date and polls in-flight orders for ~30s. Ext-hours fills lag the
activity API by more than that, so rows are left broker_status=NULL and
never re-examined on a later day (2026-06-17 17/19, 2026-06-18 15/17).

sweep_stale() re-queries the broker order status for every NULL-status row
in the last N days and updates the ones the broker has since marked
terminal. It must be:
  - idempotent (re-running does nothing once rows are reconciled),
  - non-destructive of already-reconciled rows (those aren't selected), and
  - dry-run-capable (logs, never writes).

All broker calls are mocked — no live Alpaca, no real DB.

Run:
    pytest tests/test_alpaca_reconcile_sweep.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
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
    def __init__(self, fetchall_rows=None):
        self._rows = fetchall_rows or []
        self.calls = []  # (sql, params)

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


def _order_json(order_id, status, qty='0', avg='0'):
    return json.dumps({'id': order_id, 'status': status,
                       'filled_qty': qty, 'filled_avg_price': avg})


class TestSweepStale(unittest.TestCase):
    def test_selects_only_null_status_in_window(self):
        """The sweep SELECT must filter broker_status IS NULL AND
        alpaca_order_id IS NOT NULL AND run_date within the last N days. It
        must NOT re-select reconciled rows (idempotency at the query level)."""
        conn = FakeConn(fetchall_rows=[])
        with patch('execution.alpaca_reconcile.subprocess.run'):
            ar.sweep_stale(conn, days=5)
        select_sql = conn.cur.calls[0][0]
        norm = ' '.join(select_sql.split())
        self.assertIn('broker_status IS NULL', norm)
        self.assertIn('alpaca_order_id IS NOT NULL', norm)
        self.assertIn('run_date', norm)
        # window is parameterized by N days
        self.assertEqual(conn.cur.calls[0][1], (5,))

    def test_stale_null_row_now_filled_gets_updated(self):
        conn = FakeConn(fetchall_rows=[
            ('sub-1', 'broker-ord-1', 'AAPL', 10, '2026-06-17'),
        ])
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, _order_json('broker-ord-1', 'filled', '10', '150.0'))):
            n = ar.sweep_stale(conn, days=5)
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 1)
        sql, params = update_calls[0]
        self.assertEqual(params[0], 'filled')
        self.assertEqual(params[1], 10.0)
        self.assertEqual(params[2], 150.0)
        self.assertEqual(params[3], 'sub-1')
        self.assertEqual(n, 1)
        self.assertEqual(conn.commits, 1)

    def test_stale_null_row_now_rejected_gets_marked_rejected(self):
        conn = FakeConn(fetchall_rows=[
            ('sub-2', 'broker-ord-2', 'TSLA', 5, '2026-06-17'),
        ])
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, _order_json('broker-ord-2', 'rejected'))):
            n = ar.sweep_stale(conn, days=5)
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 1)
        self.assertIn('rejected_by_broker', update_calls[0][0])
        self.assertEqual(n, 1)

    def test_still_in_flight_row_left_untouched(self):
        """fetch_order_status returns None (still new/accepted/held) → the
        sweep must leave the row alone (no UPDATE, no false-rejected)."""
        conn = FakeConn(fetchall_rows=[
            ('sub-3', 'broker-ord-3', 'MSFT', 2, '2026-06-18'),
        ])
        # 'accepted' → fetch_order_status returns None
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, _order_json('broker-ord-3', 'accepted'))):
            n = ar.sweep_stale(conn, days=5)
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 0)
        self.assertEqual(n, 0)

    def test_idempotent_second_run_is_noop(self):
        """After a sweep reconciles a row, the row's broker_status is no
        longer NULL, so the next sweep's SELECT returns it no more → zero
        UPDATEs and zero commits. We model this by an empty selection on the
        second run."""
        # First run: one stale row → filled.
        conn1 = FakeConn(fetchall_rows=[
            ('sub-1', 'broker-ord-1', 'AAPL', 10, '2026-06-17'),
        ])
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, _order_json('broker-ord-1', 'filled', '10', '150.0'))):
            ar.sweep_stale(conn1, days=5)
        # Second run: SELECT now returns nothing (row no longer NULL).
        conn2 = FakeConn(fetchall_rows=[])
        with patch('execution.alpaca_reconcile.subprocess.run') as mock_run:
            n = ar.sweep_stale(conn2, days=5)
        self.assertEqual(n, 0)
        self.assertEqual(conn2.commits, 0)
        # No broker order-get calls when there's nothing to sweep.
        mock_run.assert_not_called()
        update_calls = [c for c in conn2.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 0)

    def test_dry_run_changes_nothing(self):
        """--dry-run: logs what it WOULD do, issues no UPDATEs and no commit,
        even when the broker reports the order as terminally filled."""
        conn = FakeConn(fetchall_rows=[
            ('sub-9', 'broker-ord-9', 'NVDA', 3, '2026-06-17'),
        ])
        with patch('execution.alpaca_reconcile.subprocess.run',
                   return_value=_mock_proc(0, _order_json('broker-ord-9', 'filled', '3', '900.0'))):
            n = ar.sweep_stale(conn, days=5, dry_run=True)
        update_calls = [c for c in conn.cur.calls if 'UPDATE' in c[0]]
        self.assertEqual(len(update_calls), 0)
        self.assertEqual(conn.commits, 0)
        # Reports how many it WOULD update.
        self.assertEqual(n, 1)

    def test_empty_selection_exits_clean(self):
        conn = FakeConn(fetchall_rows=[])
        with patch('execution.alpaca_reconcile.subprocess.run') as mock_run:
            n = ar.sweep_stale(conn, days=5)
        self.assertEqual(n, 0)
        self.assertEqual(conn.commits, 0)
        mock_run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
