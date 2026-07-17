"""tests/test_sizer_action_label.py — human-readable `action` derivation."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution.regime_blended_sizer import _derive_action  # noqa: E402


class TestDeriveAction(unittest.TestCase):
    def test_open(self):
        self.assertEqual(_derive_action('delta', 0.0, 100.0, 1), 'open_long')
        self.assertEqual(_derive_action('delta', 0.0, -100.0, -1), 'open_short')

    def test_add(self):
        self.assertEqual(_derive_action('delta', 50.0, 100.0, 1), 'add_long')
        self.assertEqual(_derive_action('delta', -50.0, -100.0, -1), 'add_short')

    def test_reduce(self):
        self.assertEqual(_derive_action('delta', 100.0, 50.0, -1), 'reduce_long')
        self.assertEqual(_derive_action('delta', -100.0, -50.0, 1), 'reduce_short')

    def test_flip(self):
        self.assertEqual(_derive_action('flip_open', 0.0, 100.0, 1), 'flip_to_long')
        self.assertEqual(_derive_action('flip_open', 0.0, -100.0, -1), 'flip_to_short')

    def test_close(self):
        self.assertEqual(_derive_action('orphan_close', 100.0, 0.0, -1), 'close_long')
        self.assertEqual(_derive_action('orphan_close', -100.0, 0.0, 1), 'close_short')
        self.assertEqual(_derive_action('flip_close', 100.0, 0.0, -1), 'close_long')
        self.assertEqual(_derive_action('flip_close', -100.0, 0.0, 1), 'close_short')

    def test_glw_case_is_reduce_long(self):
        # GLW 05-27: current +$30,431 long, target +$13,307 long (smaller)
        self.assertEqual(_derive_action('delta', 30431.0, 13307.0, -1), 'reduce_long')


if __name__ == '__main__':
    unittest.main()
