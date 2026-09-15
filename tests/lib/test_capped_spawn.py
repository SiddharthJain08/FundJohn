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


class TestProbeCachingAndUidGating(unittest.TestCase):
    """Fix round 2 (task-4 review finding 2): the availability probe must
    run at most once per process (cached in _STATE['available']), and must
    gate on uid 0 — a working probe alone is not enough. `_reset(probe=,
    uid=)` injects fakes so both are exercised without ever touching a real
    systemd-run."""

    def tearDown(self):
        cs._reset()

    def test_probe_runs_exactly_once_across_two_wrap_calls(self):
        calls = []

        def _counting_probe():
            calls.append(1)
            return True

        cs._reset(probe=_counting_probe, uid=lambda: 0)
        cs.wrap_capped(['a'], memory_max='4500M')
        cs.wrap_capped(['b'], memory_max='4500M')
        self.assertEqual(len(calls), 1, calls)

    def test_uid_1001_with_working_probe_is_passthrough(self):
        cs._reset(probe=lambda: True, uid=lambda: 1001)
        argv, cap = cs.wrap_capped(['python3', 'x.py'], memory_max='4500M')
        self.assertIsNone(cap)
        self.assertEqual(argv, ['python3', 'x.py'])

    def test_uid_0_with_failing_probe_is_passthrough(self):
        cs._reset(probe=lambda: False, uid=lambda: 0)
        argv, cap = cs.wrap_capped(['python3', 'x.py'], memory_max='4500M')
        self.assertIsNone(cap)
        self.assertEqual(argv, ['python3', 'x.py'])


class TestCapValidation(unittest.TestCase):
    """Fix round 2 (task-4 review finding 3): OPENCLAW_STEP_MEMORY_MAX=4500
    (no unit) is a footgun — systemd-run reads an unsuffixed MemoryMax= as
    raw BYTES, killing every step at spawn. A malformed cap must fall back
    to DEFAULT_MEMORY_MAX with a one-time warning; '0' keeps disabling the
    cap outright (unchanged), and a properly-suffixed value passes through
    as-is."""

    def tearDown(self):
        cs._reset()

    def test_bare_digit_cap_falls_back_to_default_with_warning(self):
        logs = []
        cs._reset(available=True)
        argv, cap = cs.wrap_capped(['a'], memory_max='4500', log=logs.append)
        self.assertEqual(cap, cs.DEFAULT_MEMORY_MAX)
        self.assertEqual(len(logs), 1, logs)
        self.assertIn('4500', logs[0])

    def test_bare_digit_cap_warns_only_once(self):
        logs = []
        cs._reset(available=True)
        cs.wrap_capped(['a'], memory_max='4500', log=logs.append)
        cs.wrap_capped(['b'], memory_max='4500', log=logs.append)
        self.assertEqual(len(logs), 1, logs)

    def test_cap_with_unit_suffix_used_as_is(self):
        cs._reset(available=True)
        argv, cap = cs.wrap_capped(['a'], memory_max='4500M')
        self.assertEqual(cap, '4500M')

    def test_zero_cap_still_disables_unchanged(self):
        cs._reset(available=True)
        argv, cap = cs.wrap_capped(['a'], memory_max='0')
        self.assertIsNone(cap)
        self.assertEqual(argv, ['a'])

    def test_default_memory_max_falls_back_on_malformed_env(self):
        import os
        old = os.environ.get('OPENCLAW_STEP_MEMORY_MAX')
        os.environ['OPENCLAW_STEP_MEMORY_MAX'] = '4500'
        try:
            self.assertEqual(cs.default_memory_max(), cs.DEFAULT_MEMORY_MAX)
        finally:
            if old is None:
                os.environ.pop('OPENCLAW_STEP_MEMORY_MAX', None)
            else:
                os.environ['OPENCLAW_STEP_MEMORY_MAX'] = old

    def test_default_memory_max_keeps_zero(self):
        import os
        old = os.environ.get('OPENCLAW_STEP_MEMORY_MAX')
        os.environ['OPENCLAW_STEP_MEMORY_MAX'] = '0'
        try:
            self.assertEqual(cs.default_memory_max(), '0')
        finally:
            if old is None:
                os.environ.pop('OPENCLAW_STEP_MEMORY_MAX', None)
            else:
                os.environ['OPENCLAW_STEP_MEMORY_MAX'] = old

    def test_malformed_env_cap_warns_into_the_callers_log_via_wrap_capped(self):
        """The production path (run_step -> wrap_capped(cmd, log=log), no
        explicit memory_max) must route a malformed-env warning into the
        CALLER's log, not just stdout — `default_memory_max()` itself has
        no `log` param, but `wrap_capped` resolves the env cap through
        `_resolve_env_cap(log=log)` so this reaches the real cycle log."""
        import os
        logs = []
        old = os.environ.get('OPENCLAW_STEP_MEMORY_MAX')
        os.environ['OPENCLAW_STEP_MEMORY_MAX'] = '4500'
        try:
            cs._reset(available=True)
            argv, cap = cs.wrap_capped(['a'], log=logs.append)
        finally:
            if old is None:
                os.environ.pop('OPENCLAW_STEP_MEMORY_MAX', None)
            else:
                os.environ['OPENCLAW_STEP_MEMORY_MAX'] = old
        self.assertEqual(cap, cs.DEFAULT_MEMORY_MAX)
        self.assertEqual(len(logs), 1, logs)
        self.assertIn('4500', logs[0])


if __name__ == '__main__':
    unittest.main()
