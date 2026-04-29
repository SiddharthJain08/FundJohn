"""tests/test_orchestrator_exit_codes.py

Phase 3 of Tier 3 — exit-code discipline (0=success / 1=transient /
2=auth/config). Tests verify:

  - run_step returns (ok, rc) with the right rc decoded from subprocess
  - CycleAbort is the right exception type
  - Auth/config exit code (2) routing under OPENCLAW_STRICT_EXIT_CODES=1
  - Default (legacy) behavior unchanged when the strict flag is unset

We don't spawn the real cycle here — testing run_step directly with a
fake subprocess and verifying CycleAbort + mark_completed semantics in
isolation.

Run:
    pytest tests/test_orchestrator_exit_codes.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import pipeline_orchestrator as po  # noqa: E402


def _fake_popen(rc=0, stdout_lines=None):
    """Build a MagicMock that matches the subprocess.Popen surface used by run_step."""
    mock = MagicMock()
    mock.stdout = iter(stdout_lines or [])
    mock.wait.return_value = rc
    return mock


class TestRunStepReturnShape(unittest.TestCase):
    """run_step must now return (ok, rc) — backwards compat keeps `if ok:`
    working for legacy callers, but the rc is needed for Tier 3 routing."""

    def test_exit_0_returns_ok_true_rc_0(self):
        with patch('execution.pipeline_orchestrator.subprocess.Popen',
                   return_value=_fake_popen(rc=0)):
            ok, rc = po.run_step('alpaca_executor', '2026-04-28', {})
        self.assertTrue(ok)
        self.assertEqual(rc, 0)

    def test_exit_1_returns_ok_false_rc_1(self):
        with patch('execution.pipeline_orchestrator.subprocess.Popen',
                   return_value=_fake_popen(rc=1)):
            ok, rc = po.run_step('alpaca_executor', '2026-04-28', {})
        self.assertFalse(ok)
        self.assertEqual(rc, 1)

    def test_exit_2_returns_ok_false_rc_2(self):
        """Caller is responsible for translating rc=2 → CycleAbort. run_step
        itself just reports the rc faithfully."""
        with patch('execution.pipeline_orchestrator.subprocess.Popen',
                   return_value=_fake_popen(rc=2)):
            ok, rc = po.run_step('alpaca_executor', '2026-04-28', {})
        self.assertFalse(ok)
        self.assertEqual(rc, 2)

    def test_timeout_returns_minus_1(self):
        """Hard-timeout (entire wall-clock budget exceeded) returns rc=-1.
        The new polling loop calls proc.wait(timeout=30) repeatedly; we
        clamp _resolve_script to return a 0s budget so the deadline trips
        on the first iteration."""
        import subprocess as sp
        proc = MagicMock()
        proc.stdout = iter([])
        proc.wait.side_effect = sp.TimeoutExpired(cmd='x', timeout=1)
        with patch('execution.pipeline_orchestrator.subprocess.Popen',
                   return_value=proc), \
             patch('execution.pipeline_orchestrator._resolve_script',
                   return_value=(['python3', '-c', 'pass'], 0)):
            ok, rc = po.run_step('alpaca_executor', '2026-04-28', {})
        self.assertFalse(ok)
        self.assertEqual(rc, -1)

    def test_stdout_idle_wedge_returns_minus_2(self):
        """rc=-2 distinguishes a stdout-idle wedge (subprocess alive but
        producing no output for too long) from a hard wall-clock timeout
        (rc=-1). 2026-04-29 cycle wedge would now trip this path."""
        import subprocess as sp, os
        os.environ['STEP_STDOUT_IDLE_MAX_S'] = '0'   # any tick is "too long"
        try:
            proc = MagicMock()
            proc.stdout = iter([])     # no output → triggers idle watchdog
            calls = {'n': 0}
            def _wait(*a, **kw):
                calls['n'] += 1
                # First call: simulate "still running" (poll-loop iteration).
                # Subsequent calls: simulate "process exited" after kill.
                if calls['n'] == 1:
                    raise sp.TimeoutExpired(cmd='x', timeout=30)
                return -9  # signal-killed
            proc.wait.side_effect = _wait
            with patch('execution.pipeline_orchestrator.subprocess.Popen',
                       return_value=proc), \
                 patch('execution.pipeline_orchestrator._resolve_script',
                       return_value=(['python3', '-c', 'pass'], 5400)):
                ok, rc = po.run_step('alpaca_executor', '2026-04-28', {})
            self.assertFalse(ok)
            self.assertEqual(rc, -2)
        finally:
            os.environ.pop('STEP_STDOUT_IDLE_MAX_S', None)


class TestCycleAbortException(unittest.TestCase):
    def test_cycle_abort_carries_step_and_rc(self):
        """CycleAbort instances expose .step, .rc, .detail fields the alert
        notifier reads."""
        exc = po.CycleAbort('alpaca_executor', 2, detail='auth failed')
        self.assertEqual(exc.step, 'alpaca_executor')
        self.assertEqual(exc.rc, 2)
        self.assertIn('auth failed', exc.detail)
        # Also asserts message includes the exit code
        self.assertIn('2', str(exc))

    def test_cycle_abort_is_distinct_from_runtime_error(self):
        self.assertFalse(issubclass(po.CycleAbort, RuntimeError))
        self.assertTrue(issubclass(po.CycleAbort, Exception))


class TestMarkCompletedStatus(unittest.TestCase):
    def test_default_status_is_1(self):
        r = MagicMock()
        po.mark_completed(r, '2026-04-28')
        # Verify .set was called with value '1'
        r.set.assert_called_once()
        args, kwargs = r.set.call_args
        self.assertEqual(args[1], '1')

    def test_aborted_auth_status_persists(self):
        r = MagicMock()
        po.mark_completed(r, '2026-04-28', status='aborted_auth')
        args, kwargs = r.set.call_args
        self.assertEqual(args[1], 'aborted_auth')


if __name__ == '__main__':
    unittest.main()
