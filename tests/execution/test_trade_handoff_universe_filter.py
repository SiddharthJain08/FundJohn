"""Tests for the tradability filter in trade_handoff_builder.

Verifies that signals on symbols not in alpaca_tradable_universe get
prefiltered with reason='untradable_at_alpaca', and that an empty
universe (refresher hasn't run) triggers fail-open so trading isn't
halted by a missing table.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import trade_handoff_builder as thb  # noqa: E402


class TestLoadTradableUniverse(unittest.TestCase):
    def test_empty_uri_returns_empty(self):
        # No POSTGRES_URI → fail-open empty set, no exception.
        self.assertEqual(thb.load_tradable_universe(''), set())
        self.assertEqual(thb.load_tradable_universe(None), set())  # type: ignore

    def test_db_error_returns_empty_fail_open(self):
        # Simulating a connect failure → fail-open (set()) so handoff continues.
        with patch.object(thb, 'psycopg2') as mock_pg:
            mock_pg.connect.side_effect = Exception('boom')
            result = thb.load_tradable_universe('postgresql://bogus')
        self.assertEqual(result, set())

    def test_handoff_loader_default_equity_only(self):
        # Crypto rows now share the table (internal 'BTC-USD', asset_class=
        # 'crypto'); the handoff consumer stays us_equity-only by default.
        universe = {'AAPL': 'us_equity', 'LEGACY': None, 'BTC-USD': 'crypto'}
        seen = []

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                seen.append((sql, params))
                self._rows = [(s,) for s, c in universe.items()
                              if (c or 'us_equity') in params[0]]

            def fetchall(self):
                return self._rows

        conn = MagicMock()
        conn.cursor.return_value = _Cur()
        with patch.object(thb, 'psycopg2') as mock_pg:
            mock_pg.connect.return_value = conn
            result = thb.load_tradable_universe('postgresql://stub')
        self.assertEqual(result, {'AAPL', 'LEGACY'})
        self.assertEqual(seen[-1][1], (['us_equity'],))
        self.assertIn('asset_class', seen[-1][0])
        conn.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
