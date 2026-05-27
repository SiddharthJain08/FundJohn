"""tests/test_alpaca_executor_dtbp_guard.py — DTBP guard unit tests (mock-only)."""
from __future__ import annotations
import os
import sys, unittest
import tempfile, csv
from pathlib import Path
from unittest.mock import MagicMock, patch
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

    def test_failed_fetch_is_conservative_zero(self):
        # _fetch_account_state sets fetched=False on failure (BP fields fall
        # back to PAPER_PORTFOLIO). A failed re-fetch must NOT be permissive.
        self.assertEqual(ae._dtbp_opening_budget(
            {'fetched': False, 'daytrading_buying_power': 100000.0,
             'regt_buying_power': 100000.0}), 0.0)

    def test_fetched_true_or_absent_uses_values(self):
        # fetched=True → normal; missing 'fetched' key (e.g. unit-test dicts) → normal
        self.assertEqual(ae._dtbp_opening_budget(
            {'fetched': True, 'daytrading_buying_power': 40000.0,
             'regt_buying_power': 60000.0}), 40000.0)
        self.assertEqual(ae._dtbp_opening_budget(
            {'daytrading_buying_power': 40000.0, 'regt_buying_power': 60000.0}), 40000.0)


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


class TestRecordDtbpSkip(unittest.TestCase):
    def test_writes_skipped_dtbp_row(self):
        conn = MagicMock(); cur = MagicMock(); conn.cursor.return_value = cur
        order = {'ticker': 'AMAT', 'strategy_id': 'S_x', 'direction': 'long',
                 'pct_nav': 0.0644, 'order_class': 'bracket'}
        ae._record_dtbp_skip(conn, '2026-05-27', order, equity=110000.0)
        cur.execute.assert_called_once()
        params = cur.execute.call_args[0][1]
        self.assertIn('skipped_dtbp', params)
        self.assertIn('dtbp_budget_exhausted', params)
        conn.commit.assert_called_once()


class TestSummaryBpLine(unittest.TestCase):
    def test_bp_skip_line_counts_dtbp_skips(self):
        skipped = [{'ticker': 'AMAT', 'reason': 'dtbp_budget_exhausted'},
                   {'ticker': 'AMD', 'reason': 'dtbp_budget_exhausted'},
                   {'ticker': 'XYZ', 'reason': 'already executed'}]
        line = ae._dtbp_summary_line(skipped)
        self.assertIn('2', line)
        self.assertIn('buying-power', line.lower())
    def test_no_line_when_no_bp_skips(self):
        self.assertEqual(ae._dtbp_summary_line(
            [{'ticker': 'X', 'reason': 'already executed'}]), '')


class TestDtbpGate(unittest.TestCase):
    def test_default_on_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENCLAW_DTBP_GUARD', None)
            self.assertTrue(ae._dtbp_guard_enabled())
    def test_off_when_zero(self):
        with patch.dict(os.environ, {'OPENCLAW_DTBP_GUARD': '0'}):
            self.assertFalse(ae._dtbp_guard_enabled())
    def test_on_when_one(self):
        with patch.dict(os.environ, {'OPENCLAW_DTBP_GUARD': '1'}):
            self.assertTrue(ae._dtbp_guard_enabled())


class TestBpSnapshot(unittest.TestCase):
    def test_appends_csv_row_with_header(self):
        import tempfile, csv
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'bp_snapshots.csv'
            acct = {'equity': 110000.0, 'daytrading_buying_power': 0.0,
                    'regt_buying_power': 58000.0, 'daytrade_count': 64, 'multiplier': 4}
            ae._bp_snapshot(acct, '2026-05-27', path=str(path))
            ae._bp_snapshot(acct, '2026-05-27', path=str(path))  # append, no dup header
            rows = list(csv.reader(open(path)))
            self.assertEqual(rows[0][0], 'timestamp')   # header once
            self.assertEqual(len(rows), 3)              # header + 2 data rows
            self.assertIn('64', rows[1])                # daytrade_count present


if __name__ == '__main__':
    unittest.main()
