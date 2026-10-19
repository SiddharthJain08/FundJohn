"""tests/strategies/test_intraday_contract.py — strategies.intraday helper (pure)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies import intraday as it  # noqa: E402


class TestBars(unittest.TestCase):
    def test_thirteen_rth_bars(self):
        self.assertEqual(len(it.RTH_BAR_STARTS_ET), 13)
        self.assertEqual(it.RTH_BAR_STARTS_ET[0], '09:30')
        self.assertEqual(it.RTH_BAR_STARTS_ET[1], '10:00')
        self.assertEqual(it.RTH_BAR_STARTS_ET[-1], '15:30')
        self.assertEqual(list(it.RTH_BAR_STARTS_ET), sorted(it.RTH_BAR_STARTS_ET))


class TestSessionCloseParams(unittest.TestCase):
    def test_valid_bar_merges_extra(self):
        p = it.session_close_params('10:00', strategy_id='S_x', confidence=0.7)
        self.assertEqual(p, {'strategy_id': 'S_x', 'confidence': 0.7,
                             'exit_on': 'session_close', 'entry_bar_et': '10:00'})

    def test_every_rth_bar_is_accepted(self):
        for b in it.RTH_BAR_STARTS_ET:
            self.assertEqual(it.session_close_params(b)['entry_bar_et'], b)

    def test_invalid_bar_names_value_and_allowed_set(self):
        for bad in ('10:15', '16:00', '09:00', '9:30', '', None):
            with self.assertRaises(ValueError) as cm:
                it.session_close_params(bad)
            self.assertIn(repr(bad), str(cm.exception))
            self.assertIn('15:30', str(cm.exception))

    def test_extra_may_not_override_reserved_keys(self):
        with self.assertRaises(ValueError):
            it.session_close_params('10:00', exit_on='x')
        with self.assertRaises(ValueError):
            it.session_close_params('10:00', entry_bar_et='15:30')


class TestIsSessionClose(unittest.TestCase):
    def test_tolerates_none_and_non_dict(self):
        for v in (None, {}, [], 'session_close', 3):
            self.assertFalse(it.is_session_close(v))

    def test_other_exit_on_is_false(self):
        self.assertFalse(it.is_session_close({'exit_on': 'target'}))

    def test_opted_in(self):
        self.assertTrue(it.is_session_close(it.session_close_params('15:30')))


if __name__ == '__main__':
    unittest.main()
