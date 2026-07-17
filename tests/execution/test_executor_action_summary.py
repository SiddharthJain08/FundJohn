"""tests/test_executor_action_summary.py — #trade-reports shows action label."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import alpaca_executor as ae  # noqa: E402


class TestSubmittedSample(unittest.TestCase):
    def test_shows_action_not_direction(self):
        s = ae._format_submitted_sample([{'ticker': 'GLW', 'action': 'reduce_long', 'qty': 91}])
        self.assertEqual(s, 'GLW:reduce_long x91')

    def test_falls_back_to_direction_when_no_action(self):
        s = ae._format_submitted_sample([{'ticker': 'AAPL', 'direction': 'long', 'qty': 10}])
        self.assertEqual(s, 'AAPL:long x10')

    def test_truncates_to_limit(self):
        rows = [{'ticker': f'T{i}', 'action': 'open_long', 'qty': 1} for i in range(12)]
        s = ae._format_submitted_sample(rows, limit=8)
        self.assertEqual(len(s.split(', ')), 8)


if __name__ == '__main__':
    unittest.main()
