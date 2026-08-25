"""Tests for src/backtest/eligibility_assigner.py.

DB layer mocked — focus on the rule logic (sharpe + trade_count
thresholds, refuse-to-wipe behaviour, canonical regime ordering).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
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


def _mock_conn_failopen(run_id='run-uuid', regime_rows=None, exc=None):
    """Like _mock_conn, but the SECOND execute() call (the
    benchmark_sharpe-augmented SELECT; the FIRST is the run_id lookup)
    raises -- simulates a pre-migration-149 DB missing the column.
    compute_eligible must catch it, roll back, and retry without the
    column. Returns (conn, cur) so tests can assert on call counts."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = {'run_id': run_id} if run_id else None
    cur.__iter__ = lambda self: iter(regime_rows or [])
    calls = {'n': 0}

    def _execute_side_effect(sql, params=None):
        calls['n'] += 1
        if calls['n'] == 2:
            raise (exc or RuntimeError('column "benchmark_sharpe" does not exist'))

    cur.execute.side_effect = _execute_side_effect
    conn.cursor.return_value = cur
    return conn, cur


class TestComputeEligible(unittest.TestCase):
    def test_picks_only_regimes_clearing_both_thresholds(self):
        rows = [
            {'regime_state': 'LOW_VOL',       'sharpe': 0.5,  'trade_count': 100, 'max_dd_pct': 10.0},
            {'regime_state': 'TRANSITIONING', 'sharpe': 0.3,  'trade_count': 100, 'max_dd_pct': 10.0},  # fails sharpe (override 0.4)
            {'regime_state': 'HIGH_VOL',      'sharpe': 0.8,  'trade_count': 10,  'max_dd_pct': 10.0},  # fails trades
            {'regime_state': 'CRISIS',        'sharpe': 1.2,  'trade_count': 60,  'max_dd_pct': 10.0},  # passes
        ]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_test', min_sharpe=0.4, min_trades=20)
        # Canonical order: LOW_VOL, TRANSITIONING, HIGH_VOL, CRISIS
        self.assertEqual(eligible, ['LOW_VOL', 'CRISIS'])

    def test_default_rule_positive_sharpe_100_trades(self):
        # 2026-07-13 v2 defaults: sharpe strictly > 0, trades >= 100.
        rows = [
            {'regime_state': 'LOW_VOL',  'sharpe': 0.01, 'trade_count': 100, 'max_dd_pct': 10.0},
            {'regime_state': 'HIGH_VOL', 'sharpe': 0.0,  'trade_count': 500, 'max_dd_pct': 10.0},  # sharpe not > 0
            {'regime_state': 'CRISIS',   'sharpe': 2.0,  'trade_count': 99,  'max_dd_pct': 10.0},  # trades < 100
        ]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_test')
        self.assertEqual(eligible, ['LOW_VOL'])

    def test_sleeve_dd_ceiling_class_aware(self):
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 1.0, 'trade_count': 200, 'max_dd_pct': 25.0}]
        conn = _mock_conn(regime_rows=rows)
        eligible, _ = ea.compute_eligible(conn, 'S_test')  # equity ceiling 20
        self.assertEqual(eligible, [])
        conn = _mock_conn(regime_rows=rows)
        eligible, _ = ea.compute_eligible(conn, 'S_test', instrument_class='option')  # 30
        self.assertEqual(eligible, ['LOW_VOL'])
        rows2 = [{'regime_state': 'LOW_VOL', 'sharpe': 1.0, 'trade_count': 200, 'max_dd_pct': None}]
        conn = _mock_conn(regime_rows=rows2)
        eligible, diag = ea.compute_eligible(conn, 'S_test')  # NULL dd fails closed
        self.assertEqual(eligible, [])

    def test_no_run_returns_empty(self):
        conn = _mock_conn(run_id=None)
        eligible, diag = ea.compute_eligible(conn, 'S_test')
        self.assertEqual(eligible, [])
        self.assertEqual(diag, {})

    def test_null_sharpe_excludes(self):
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': None, 'trade_count': 200, 'max_dd_pct': 10.0}]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_test')
        self.assertEqual(eligible, [])
        self.assertFalse(diag['LOW_VOL']['eligible'])

    def test_sharpe_boundary_strict_trades_inclusive(self):
        # Sharpe must STRICTLY exceed min_sharpe; trade floor stays inclusive.
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 0.4, 'trade_count': 20, 'max_dd_pct': 10.0}]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_test', min_sharpe=0.4, min_trades=20)
        self.assertEqual(eligible, [])
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 0.41, 'trade_count': 20, 'max_dd_pct': 10.0}]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_test', min_sharpe=0.4, min_trades=20)
        self.assertEqual(eligible, ['LOW_VOL'])


# ── compute_eligible: R1-assigners benchmark leg (2026-08-25) ───────────────
class TestComputeEligibleBenchmarkLeg(unittest.TestCase):
    def test_null_benchmark_is_a_noop_legacy_pass_stands(self):
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 1.2, 'trade_count': 150,
                'max_dd_pct': 10.0, 'benchmark_sharpe': None}]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_ZZT_test')
        self.assertEqual(eligible, ['LOW_VOL'])

    def test_missing_benchmark_key_is_also_a_noop(self):
        # No 'benchmark_sharpe' key at all (e.g. a pre-R1 fixture) is
        # treated identically to an explicit None.
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 1.2, 'trade_count': 150, 'max_dd_pct': 10.0}]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_ZZT_test')
        self.assertEqual(eligible, ['LOW_VOL'])

    def test_benchmark_flips_an_otherwise_passing_regime_to_ineligible(self):
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 0.6, 'trade_count': 150,
                'max_dd_pct': 10.0, 'benchmark_sharpe': 0.9}]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_ZZT_test')
        self.assertEqual(eligible, [])
        self.assertFalse(diag['LOW_VOL']['eligible'])

    def test_benchmark_leaves_a_clearing_regime_eligible(self):
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 1.5, 'trade_count': 150,
                'max_dd_pct': 10.0, 'benchmark_sharpe': 0.9}]
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_ZZT_test')
        self.assertEqual(eligible, ['LOW_VOL'])

    def test_benchmark_never_rescues_a_legacy_failure(self):
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 5.0, 'trade_count': 5,
                'max_dd_pct': 10.0, 'benchmark_sharpe': -5.0}]  # trades < 100
        conn = _mock_conn(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_ZZT_test')
        self.assertEqual(eligible, [])

    def test_missing_benchmark_column_fails_open_not_crash(self):
        """Pre-migration-149 DB: the benchmark_sharpe-augmented SELECT
        raises. Must not crash the caller -- rolls back and retries without
        the column; the legacy-only verdict stands."""
        rows = [{'regime_state': 'LOW_VOL', 'sharpe': 1.2, 'trade_count': 150, 'max_dd_pct': 10.0}]
        conn, cur = _mock_conn_failopen(regime_rows=rows)
        eligible, diag = ea.compute_eligible(conn, 'S_ZZT_test')
        self.assertEqual(eligible, ['LOW_VOL'])
        conn.rollback.assert_called_once()
        self.assertEqual(cur.execute.call_count, 3)  # run_id, raising query, fallback retry


if __name__ == '__main__':
    unittest.main()
