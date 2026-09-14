"""tests/lib/test_capped_spawn.py — Python twin of capped_spawn.js (QD §5 E2).

Never probes the host: _reset() pins availability so both branches are
exercised deterministically, and no systemd-run is ever executed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import capped_spawn as cs  # noqa: E402


class TestDefaults(unittest.TestCase):
    def tearDown(self):
        cs._reset()

    def test_default_cap_is_4500M(self):
        self.assertEqual(cs.DEFAULT_MEMORY_MAX, '4500M')
        self.assertEqual(cs.MEMORY_MAX_ENV, 'OPENCLAW_STEP_MEMORY_MAX')

    def test_env_overrides_the_default(self):
        import os
        old = os.environ.get('OPENCLAW_STEP_MEMORY_MAX')
        os.environ['OPENCLAW_STEP_MEMORY_MAX'] = '2G'
        try:
            self.assertEqual(cs.default_memory_max(), '2G')
        finally:
            if old is None:
                del os.environ['OPENCLAW_STEP_MEMORY_MAX']
            else:
                os.environ['OPENCLAW_STEP_MEMORY_MAX'] = old

    def test_it_does_not_read_the_backtest_cap_var(self):
        import os
        old_step = os.environ.pop('OPENCLAW_STEP_MEMORY_MAX', None)
        old_bt = os.environ.get('OPENCLAW_BACKTEST_MEMORY_MAX')
        os.environ['OPENCLAW_BACKTEST_MEMORY_MAX'] = '999M'
        try:
            self.assertEqual(cs.default_memory_max(), '4500M')
        finally:
            if old_step is not None:
                os.environ['OPENCLAW_STEP_MEMORY_MAX'] = old_step
            if old_bt is None:
                del os.environ['OPENCLAW_BACKTEST_MEMORY_MAX']
            else:
                os.environ['OPENCLAW_BACKTEST_MEMORY_MAX'] = old_bt


class TestWrap(unittest.TestCase):
    def tearDown(self):
        cs._reset()

    def test_wraps_in_a_transient_scope_when_available(self):
        cs._reset(available=True)
        argv, cap = cs.wrap_capped(['python3', 'x.py', '--date', '2026-09-14'], memory_max='4500M')
        self.assertEqual(cap, '4500M')
        self.assertEqual(argv, ['systemd-run', '--scope', '--collect', '--quiet',
                                '-p', 'MemoryMax=4500M', '--',
                                'python3', 'x.py', '--date', '2026-09-14'])

    def test_passes_through_untouched_when_unavailable(self):
        cs._reset(available=False)
        argv, cap = cs.wrap_capped(['python3', 'x.py'], memory_max='4500M')
        self.assertIsNone(cap)
        self.assertEqual(argv, ['python3', 'x.py'])

    def test_zero_cap_disables_even_when_available(self):
        cs._reset(available=True)
        argv, cap = cs.wrap_capped(['node', 'y.js'], memory_max='0')
        self.assertIsNone(cap)
        self.assertEqual(argv, ['node', 'y.js'])

    def test_empty_cap_disables(self):
        cs._reset(available=True)
        argv, cap = cs.wrap_capped(['node', 'y.js'], memory_max='   ')
        self.assertIsNone(cap)
        self.assertEqual(argv, ['node', 'y.js'])

    def test_returns_a_copy_never_the_caller_list(self):
        cs._reset(available=False)
        original = ['python3', 'x.py']
        argv, _cap = cs.wrap_capped(original)
        argv.append('--mutated')
        self.assertEqual(original, ['python3', 'x.py'])

    def test_warns_once_when_unavailable(self):
        logs = []
        cs._reset()
        cs._STATE['probe'] = lambda: False
        cs._STATE['uid'] = lambda: 1001
        cs.wrap_capped(['a'], log=logs.append)
        cs.wrap_capped(['b'], log=logs.append)
        self.assertEqual(len(logs), 1, logs)
        self.assertIn('uid=1001', logs[0])


if __name__ == '__main__':
    unittest.main()
