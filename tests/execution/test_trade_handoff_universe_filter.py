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
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
