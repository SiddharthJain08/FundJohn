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


if __name__ == '__main__':
    unittest.main()
