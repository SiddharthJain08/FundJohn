"""Tests for src/maintenance/refresh_tradable_universe.py.

Mocks out the alpaca CLI subprocess and exercises the upsert/diff
logic against an in-memory dict view of the table state.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import refresh_tradable_universe as rtu  # noqa: E402


def _mock_cursor(prior_rows):
    """Build a cursor mock whose first SELECT returns prior_rows and which
    accepts subsequent execute() calls silently."""
    cur = MagicMock()
    # First SELECT returns prior state — list of {symbol, tradable, status}.
    cur.__iter__ = lambda self: iter(prior_rows)
    cur.fetchall = lambda: list(prior_rows)
    return cur


def _asset(symbol, tradable=True, shortable=True):
    return {
        'symbol': symbol, 'id': f'id-{symbol}', 'name': symbol,
        'class': 'us_equity', 'exchange': 'NYSE', 'status': 'active',
        'tradable': tradable, 'shortable': shortable,
        'marginable': True, 'fractionable': False, 'easy_to_borrow': False,
    }


class TestDiffStats(unittest.TestCase):
    def test_first_run_marks_all_as_newly_listed(self):
        conn = MagicMock()
        prior_cursor = _mock_cursor([])
        conn.cursor.return_value = prior_cursor
        # execute_values is called on the module, not the cursor
        with patch.object(rtu.psycopg2.extras, 'execute_values'):
            stats = rtu.upsert_universe(conn, [_asset('AAA'), _asset('BBB')])
        self.assertEqual(stats['total_active'], 2)
        self.assertEqual(stats['total_tradable'], 2)
        self.assertEqual(stats['newly_listed'], 2)
        self.assertEqual(stats['newly_inactive'], 0)
        self.assertEqual(stats['status_changes'], 0)

    def test_dropped_symbol_marked_newly_inactive(self):
        conn = MagicMock()
        prior_rows = [
            {'symbol': 'AAA', 'tradable': True,  'status': 'active'},
            {'symbol': 'GONE', 'tradable': True, 'status': 'active'},
        ]
        conn.cursor.return_value = _mock_cursor(prior_rows)
        with patch.object(rtu.psycopg2.extras, 'execute_values'):
            stats = rtu.upsert_universe(conn, [_asset('AAA')])
        self.assertEqual(stats['newly_inactive'], 1)
        self.assertEqual(stats['newly_listed'], 0)

    def test_status_change_detected(self):
        # Prior had AAA tradable; today AAA arrives non-tradable.
        conn = MagicMock()
        prior_rows = [{'symbol': 'AAA', 'tradable': True, 'status': 'active'}]
        conn.cursor.return_value = _mock_cursor(prior_rows)
        with patch.object(rtu.psycopg2.extras, 'execute_values'):
            stats = rtu.upsert_universe(conn, [_asset('AAA', tradable=False)])
        self.assertEqual(stats['status_changes'], 1)
        self.assertEqual(stats['newly_listed'], 0)
        self.assertEqual(stats['newly_inactive'], 0)

    def test_dry_run_skips_writes(self):
        conn = MagicMock()
        conn.cursor.return_value = _mock_cursor([])
        with patch.object(rtu.psycopg2.extras, 'execute_values') as bulk:
            stats = rtu.upsert_universe(conn, [_asset('AAA')], dry_run=True)
        bulk.assert_not_called()
        self.assertTrue(stats['dry_run'])


class TestFetchAlpacaAssets(unittest.TestCase):
    def test_raises_on_cli_failure(self):
        proc = MagicMock(returncode=1, stdout='', stderr='no auth')
        with patch.object(rtu.subprocess, 'run', return_value=proc):
            import subprocess
            with self.assertRaises(subprocess.CalledProcessError):
                rtu.fetch_alpaca_assets()

    def test_raises_on_error_dict(self):
        import json as _json
        proc = MagicMock(returncode=0,
                         stdout=_json.dumps({'error': 'authentication required'}),
                         stderr='')
        with patch.object(rtu.subprocess, 'run', return_value=proc):
            with self.assertRaises(RuntimeError):
                rtu.fetch_alpaca_assets()

    def test_returns_assets_on_success(self):
        import json as _json
        proc = MagicMock(returncode=0,
                         stdout=_json.dumps([_asset('AAA'), _asset('BBB')]),
                         stderr='')
        with patch.object(rtu.subprocess, 'run', return_value=proc):
            assets, ms = rtu.fetch_alpaca_assets()
        self.assertEqual(len(assets), 2)
        self.assertGreaterEqual(ms, 0)


if __name__ == '__main__':
    unittest.main()
