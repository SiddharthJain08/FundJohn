"""tests/test_bt_sharpe_clamp.py — interim backtest-Sharpe plausibility clamp
(§7 metric recon). Verifies the pipeline_config reader defaults safely and the
clamp is sign-preserving and disable-able."""
from __future__ import annotations
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import strategy_weights as sw  # noqa: E402


class FakeCur:
    """Minimal cursor stub: fetchone returns the queued row; optionally raises."""
    def __init__(self, row=None, raise_on_execute=False):
        self._row = row
        self._raise = raise_on_execute
    def execute(self, *a, **k):
        if self._raise:
            raise RuntimeError('boom')
    def fetchone(self):
        return self._row


class TestGetCap(unittest.TestCase):
    def test_present_value_parsed(self):
        self.assertEqual(sw._get_bt_sharpe_cap(FakeCur(row=('2.5',))), 2.5)
    def test_absent_key_defaults_3(self):
        self.assertEqual(sw._get_bt_sharpe_cap(FakeCur(row=None)), 3.0)
    def test_malformed_value_defaults_3(self):
        self.assertEqual(sw._get_bt_sharpe_cap(FakeCur(row=('abc',))), 3.0)
    def test_query_error_defaults_3(self):
        self.assertEqual(sw._get_bt_sharpe_cap(FakeCur(raise_on_execute=True)), 3.0)


class TestClamp(unittest.TestCase):
    def test_inflated_positive_capped(self):
        out = {('A', 'LOW_VOL'): {'bt_sharpe': 14.0, 'bt_n': 100}}
        clamped = sw._clamp_bt_sharpes(out, 3.0)
        self.assertEqual(out[('A', 'LOW_VOL')]['bt_sharpe'], 3.0)
        self.assertEqual(clamped, [(('A', 'LOW_VOL'), 14.0, 3.0)])

    def test_inflated_negative_capped_still_negative(self):
        out = {('B', 'LOW_VOL'): {'bt_sharpe': -8.57, 'bt_n': 100}}
        sw._clamp_bt_sharpes(out, 3.0)
        self.assertEqual(out[('B', 'LOW_VOL')]['bt_sharpe'], -3.0)  # still < 0 -> excluded

    def test_within_band_untouched(self):
        out = {('C', 'LOW_VOL'): {'bt_sharpe': 1.2, 'bt_n': 50}}
        self.assertEqual(sw._clamp_bt_sharpes(out, 3.0), [])
        self.assertEqual(out[('C', 'LOW_VOL')]['bt_sharpe'], 1.2)

    def test_none_untouched(self):
        out = {('D', 'LOW_VOL'): {'bt_sharpe': None, 'bt_n': None}}
        self.assertEqual(sw._clamp_bt_sharpes(out, 3.0), [])
        self.assertIsNone(out[('D', 'LOW_VOL')]['bt_sharpe'])

    def test_nonfinite_untouched(self):
        out = {('E', 'HIGH_VOL'): {'bt_sharpe': float('nan'), 'bt_n': 5},
               ('F', 'HIGH_VOL'): {'bt_sharpe': float('inf'), 'bt_n': 5}}
        self.assertEqual(sw._clamp_bt_sharpes(out, 3.0), [])
        self.assertTrue(math.isnan(out[('E', 'HIGH_VOL')]['bt_sharpe']))
        self.assertTrue(math.isinf(out[('F', 'HIGH_VOL')]['bt_sharpe']))

    def test_disable_via_high_cap(self):
        out = {('G', 'LOW_VOL'): {'bt_sharpe': 14.0, 'bt_n': 100}}
        self.assertEqual(sw._clamp_bt_sharpes(out, 999.0), [])
        self.assertEqual(out[('G', 'LOW_VOL')]['bt_sharpe'], 14.0)

    def test_sign_never_crosses_zero(self):
        out = {('H', 'LOW_VOL'): {'bt_sharpe': 14.0, 'bt_n': 1},
               ('I', 'LOW_VOL'): {'bt_sharpe': -8.5, 'bt_n': 1}}
        for key, before, after in sw._clamp_bt_sharpes(out, 3.0):
            self.assertTrue((before > 0) == (after > 0) and (before < 0) == (after < 0))


if __name__ == '__main__':
    unittest.main()
