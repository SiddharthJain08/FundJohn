"""Tests for src/backtest/eligibility_assigner.py.

DB layer mocked — focus on the rule logic (sharpe + trade_count
thresholds, refuse-to-wipe behaviour, canonical regime ordering).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import eligibility_assigner as ea  # noqa: E402


def _mock_conn(run_id='run-uuid', regime_rows=None):
    """Build a conn whose first SELECT fetches the run, and subsequent
    iteration returns regime_rows."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = {'run_id': run_id} if run_id else None
    cur.__iter__ = lambda self: iter(regime_rows or [])
    conn.cursor.return_value = cur
    return conn


class TestComputeEligible(unittest.TestCase):
    def test_picks_only_regimes_clearing_both_thresholds(self):
        rows = [
            {'regime_state': 'LOW_VOL',       'sharpe': 0.5,  'trade_count': 100},
            {'regime_state': 'TRANSITIONING', 'sharpe': 0.3,  'trade_count': 100},  # fails sharpe
            {'regime_state': 'HIGH_VOL',      'sharpe': 0.8,  'trade_count': 10},   # fails trades
            {'regime_state': 'CRISIS',        'sharpe': 1.2,  'trade_count': 60},   # passes
        ]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_test', min_sharpe=0.4, min_trades=20)
        # Canonical order: LOW_VOL, TRANSITIONING, HIGH_VOL, CRISIS
        self.assertEqual(eligible, ['LOW_VOL', 'CRISIS'])

    def test_no_run_returns_empty(self):
        conn = _mock_conn(run_id=None)
        eligible, diag = ea.compute_eligible(conn, 'S_test')
        self.assertEqual(eligible, [])
        self.assertEqual(diag, {})

    def test_null_sharpe_excludes(self):
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': None, 'trade_count': 200}]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_test')
        self.assertEqual(eligible, [])
        self.assertFalse(diag['LOW_VOL']['eligible'])

    def test_threshold_boundaries_inclusive(self):
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 0.4, 'trade_count': 20}]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_test', min_sharpe=0.4, min_trades=20)
        self.assertEqual(eligible, ['LOW_VOL'])


if __name__ == '__main__':
    unittest.main()
