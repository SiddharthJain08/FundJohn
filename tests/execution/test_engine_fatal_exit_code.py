"""tests/test_engine_fatal_exit_code.py — engine fatal exit-code mapping.

CloseProxyError must exit 2 (graph aborts regardless of strict mode); other
errors keep rc=1. Guards against the empty-signals→liquidation path when the
close[t]-proxy snapshot fails.
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import engine  # noqa: E402
from ingestion.close_proxy_snapshot import CloseProxyError  # noqa: E402


class TestFatalExitCode(unittest.TestCase):
    def test_close_proxy_error_exits_2(self):
        self.assertEqual(engine._fatal_exit_code(CloseProxyError('fetch failed')), 2)

    def test_other_errors_exit_1(self):
        self.assertEqual(engine._fatal_exit_code(ValueError('x')), 1)
        self.assertEqual(engine._fatal_exit_code(RuntimeError('x')), 1)
        self.assertEqual(engine._fatal_exit_code(Exception('x')), 1)


if __name__ == '__main__':
    unittest.main()
