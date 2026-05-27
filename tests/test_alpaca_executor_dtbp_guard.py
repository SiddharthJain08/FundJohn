"""tests/test_alpaca_executor_dtbp_guard.py — DTBP guard unit tests (mock-only)."""
from __future__ import annotations
import sys, unittest
from pathlib import Path
from unittest.mock import MagicMock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import alpaca_executor as ae  # noqa: E402


class TestDtbpBudget(unittest.TestCase):
    def test_budget_is_min_of_dtbp_and_regt_floored_at_zero(self):
        self.assertEqual(ae._dtbp_opening_budget(
            {'daytrading_buying_power': 0.0, 'regt_buying_power': 58000.0}), 0.0)
        self.assertEqual(ae._dtbp_opening_budget(
            {'daytrading_buying_power': 90000.0, 'regt_buying_power': 58000.0}), 58000.0)
        self.assertEqual(ae._dtbp_opening_budget(
            {'daytrading_buying_power': -5.0, 'regt_buying_power': 100.0}), 0.0)
        self.assertEqual(ae._dtbp_opening_budget({}), 0.0)


class TestComputeDtbpSkips(unittest.TestCase):
    def _open(self, tkr, pct, kelly, sid=None):
        return {'ticker': tkr, 'strategy_id': sid or tkr,
                'direction': 'long', 'pct_nav': pct, 'kelly_final': kelly}

    def test_skips_lowest_conviction_when_budget_tight(self):
        opens = [self._open('A', 0.30, 0.30), self._open('B', 0.30, 0.20),
                 self._open('C', 0.30, 0.10)]
        acct = {'daytrading_buying_power': 65000.0, 'regt_buying_power': 999999.0}
        self.assertEqual(ae._compute_dtbp_skips(opens, acct, equity=100000.0), {('C', 'C')})

    def test_stop_and_skip_all_after_first_nonfit(self):
        opens = [self._open('A', 0.30, 0.30), self._open('B', 0.05, 0.20),
                 self._open('C', 0.30, 0.10)]
        acct = {'daytrading_buying_power': 31000.0, 'regt_buying_power': 999999.0}
        self.assertEqual(ae._compute_dtbp_skips(opens, acct, equity=100000.0),
                         {('B', 'B'), ('C', 'C')})

    def test_budget_zero_skips_all(self):
        opens = [self._open('A', 0.30, 0.30), self._open('B', 0.30, 0.20)]
        acct = {'daytrading_buying_power': 0.0, 'regt_buying_power': 50000.0}
        self.assertEqual(ae._compute_dtbp_skips(opens, acct, equity=100000.0),
                         {('A', 'A'), ('B', 'B')})

    def test_no_opens_no_skips(self):
        self.assertEqual(ae._compute_dtbp_skips([], {'daytrading_buying_power': 0.0,
                         'regt_buying_power': 0.0}, equity=100000.0), set())

    def test_ample_budget_skips_nothing(self):
        opens = [self._open('A', 0.30, 0.30), self._open('B', 0.30, 0.20)]
        acct = {'daytrading_buying_power': 999999.0, 'regt_buying_power': 999999.0}
        self.assertEqual(ae._compute_dtbp_skips(opens, acct, equity=100000.0), set())


if __name__ == '__main__':
    unittest.main()
