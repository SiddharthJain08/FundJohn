"""tests/test_pipeline_orchestrator_steps.py

Phase 0 of the regime-redeploy-not-liquidate plan: coverage for the
`--steps` and `--reason` flags added to pipeline_orchestrator.main().

Tests follow the same unittest+patch style as test_orchestrator_exit_codes.py.
We never spawn real subprocesses or talk to real Redis — every external
dependency is mocked at the module level so the suite runs in <1s.

Run:
    pytest tests/test_pipeline_orchestrator_steps.py -v
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import pipeline_orchestrator as po  # noqa: E402


# A fixed, minimal STEPS list used by the harness so tests don't drift
# when the production STEPS list grows. The order MUST be respected.
FAKE_STEPS = [
    ('collect',   'run_collector_once'),
    ('signals',   'engine'),
    ('handoff',   'trade_handoff_builder'),
    ('trade',     'trade_agent'),
    ('alpaca',    'alpaca_executor'),
    ('reconcile', 'alpaca_reconcile'),
    ('report',    'send_report'),
    ('health',    'daily_health_digest'),
]


def _fake_redis():
    """Build a MagicMock that behaves like a Redis client just enough for
    main(): set(nx=True) returns truthy (lock acquired), get() returns
    None (no checkpoint, no completion sentinel)."""
    r = MagicMock()
    r.set.return_value = True            # acquire_lock True
    r.get.return_value = None            # no checkpoint, no completion
    return r


def _run_main(argv, *, doctor_rc=0, step_rc=0, postgres_uri='postgresql://x',
              extra_env=None):
    """Execute po.main(argv) under a full mock harness. Returns
    (exit_code, captured_stdout, scheduled_steps, doctor_invocations,
    pipeline_feed_msgs, notify_msgs)."""
    scheduled_steps: list[tuple[str, str]] = []
    doctor_invocations: list[list] = []
    pipeline_feed_msgs: list[str] = []
    notify_msgs: list[str] = []

    def _fake_run_step(script, run_date, env):
        scheduled_steps.append((script, run_date))
        return (step_rc == 0, step_rc)

    def _fake_subprocess_run(*args, **kwargs):
        # Doctor is the only subprocess.run callsite in main() — capture it
        # so tests can assert it was/wasn't invoked.
        doctor_invocations.append(list(args[0]) if args else [])
        result = MagicMock()
        result.returncode = doctor_rc
        result.stdout = '{"checks": []}'
        return result

    def _fake_pipeline_feed(msg):
        pipeline_feed_msgs.append(msg)

    def _fake_notify(msg, channel='pipeline-feed'):
        notify_msgs.append(msg)

    env_overrides = {'POSTGRES_URI': postgres_uri}
    if extra_env:
        env_overrides.update(extra_env)

    buf = io.StringIO()
    with patch.dict('os.environ', env_overrides, clear=False), \
         patch('execution.pipeline_orchestrator.get_redis',
               return_value=_fake_redis()), \
         patch('execution.pipeline_orchestrator.run_step',
               side_effect=_fake_run_step), \
         patch('execution.pipeline_orchestrator.subprocess.run',
               side_effect=_fake_subprocess_run), \
         patch('execution.pipeline_orchestrator.pipeline_feed',
               side_effect=_fake_pipeline_feed), \
         patch('execution.pipeline_orchestrator.notify',
               side_effect=_fake_notify), \
         patch('execution.pipeline_orchestrator.set_agent_status'), \
         patch('execution.pipeline_orchestrator.broadcast_dashboard_refresh'), \
         patch('execution.pipeline_orchestrator._emit_maintenance_report'), \
         patch('execution.pipeline_orchestrator.check_signal_quality',
               return_value=(True, 'ok')), \
         patch('execution.pipeline_orchestrator.check_budget',
               return_value=(True, 'ok')), \
         patch('execution.pipeline_orchestrator.STEPS', FAKE_STEPS):
        with redirect_stdout(buf):
            rc = po.main(argv)

    return rc, buf.getvalue(), scheduled_steps, doctor_invocations, \
        pipeline_feed_msgs, notify_msgs


class TestFilterSteps(unittest.TestCase):
    """filter_steps is a pure helper — test it directly without spinning up
    the whole main() machine."""

    def test_none_returns_full_list(self):
        out, err = po.filter_steps(FAKE_STEPS, None)
        self.assertEqual(out, FAKE_STEPS)
        self.assertEqual(err, '')

    def test_empty_string_returns_full_list(self):
        out, err = po.filter_steps(FAKE_STEPS, '')
        self.assertEqual(out, FAKE_STEPS)
        self.assertEqual(err, '')

    def test_subset_preserves_canonical_order(self):
        # Operator types in random order; output follows STEPS order.
        out, err = po.filter_steps(FAKE_STEPS, 'alpaca,signals,trade')
        self.assertEqual([k for k, _ in out], ['signals', 'trade', 'alpaca'])
        self.assertEqual(err, '')

    def test_unknown_step_names_in_error(self):
        out, err = po.filter_steps(FAKE_STEPS, 'signals,bogus,trade,nope')
        self.assertEqual(out, [])
        self.assertIn('bogus', err)
        self.assertIn('nope', err)


class TestDefaultBehavior(unittest.TestCase):
    """When neither flag is set, the orchestrator runs every step in the
    canonical STEPS list. Zero behavior change for the 10 AM cron."""

    def test_default_runs_all_steps(self):
        rc, _, scheduled, _, _, _ = _run_main(['--date', '2026-05-19'])
        self.assertEqual(rc, 0)
        scripts = [s for s, _ in scheduled]
        expected = [script for _, script in FAKE_STEPS]
        self.assertEqual(scripts, expected)


class TestStepsFlag(unittest.TestCase):
    def test_subset_filters_steps(self):
        rc, _, scheduled, _, _, _ = _run_main([
            '--date', '2026-05-19',
            '--steps', 'signals,handoff,trade,alpaca,reconcile',
        ])
        self.assertEqual(rc, 0)
        scripts = [s for s, _ in scheduled]
        self.assertEqual(scripts, [
            'engine', 'trade_handoff_builder', 'trade_agent',
            'alpaca_executor', 'alpaca_reconcile',
        ])

    def test_unknown_step_exits_2(self):
        rc, out, scheduled, _, _, _ = _run_main([
            '--date', '2026-05-19',
            '--steps', 'signals,bogus,trade',
        ])
        self.assertEqual(rc, 2)
        # No steps were scheduled — config error short-circuits before
        # anything runs.
        self.assertEqual(scheduled, [])
        self.assertIn('bogus', out)

    def test_steps_order_matches_canonical_not_input_order(self):
        # Operator typed alpaca,signals,trade — orchestrator must run them
        # as signals, trade, alpaca.
        rc, _, scheduled, _, _, _ = _run_main([
            '--date', '2026-05-19',
            '--steps', 'alpaca,signals,trade',
        ])
        self.assertEqual(rc, 0)
        scripts = [s for s, _ in scheduled]
        self.assertEqual(scripts, ['engine', 'trade_agent', 'alpaca_executor'])


class TestReasonFlag(unittest.TestCase):
    def test_reason_propagates(self):
        rc, out, _, _, feed_msgs, _ = _run_main([
            '--date', '2026-05-19',
            '--steps', 'signals',
            '--reason', 'INTRADAY_TEST_FOO',
        ])
        self.assertEqual(rc, 0)
        # Cycle-start log line must include the reason
        self.assertIn('INTRADAY_TEST_FOO', out)
        # Every Discord pipeline_feed post must include the reason prefix
        self.assertTrue(feed_msgs, 'expected at least one pipeline_feed post')
        for msg in feed_msgs:
            self.assertIn('INTRADAY_TEST_FOO', msg,
                          f'reason missing from pipeline_feed msg: {msg!r}')

    def test_reason_clamped_to_80_chars(self):
        long_reason = 'A' * 200
        rc, out, _, _, feed_msgs, _ = _run_main([
            '--date', '2026-05-19',
            '--steps', 'signals',
            '--reason', long_reason,
        ])
        self.assertEqual(rc, 0)
        # Should be truncated; tag is bracketed so any one msg contains
        # the clamped 80-char form at most.
        self.assertTrue(any('A' * 80 in msg for msg in feed_msgs))
        self.assertFalse(any('A' * 100 in msg for msg in feed_msgs),
                         'reason was not clamped to 80 chars')


class TestDoctorSkip(unittest.TestCase):
    """When --steps excludes 'collect', the 30s doctor pre-flight is
    bypassed (data already fresh). When 'collect' is in the subset, the
    pre-flight still runs."""

    def test_doctor_skipped_when_collect_absent(self):
        rc, out, _, doctor_calls, _, _ = _run_main([
            '--date', '2026-05-19',
            '--steps', 'signals,handoff,trade,alpaca,reconcile',
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(doctor_calls, [],
                         f'doctor subprocess.run unexpectedly invoked: {doctor_calls}')
        self.assertIn('doctor skipped', out)

    def test_doctor_still_runs_when_collect_present(self):
        rc, _, _, doctor_calls, _, _ = _run_main([
            '--date', '2026-05-19',
            '--steps', 'collect,signals,handoff',
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(len(doctor_calls), 1,
                         f'expected exactly one doctor invocation, got: {doctor_calls}')
        # The subprocess.run argv should include doctor.py
        self.assertTrue(any('doctor.py' in str(arg)
                            for arg in doctor_calls[0]),
                        f'doctor.py not in invocation argv: {doctor_calls[0]}')

    def test_doctor_runs_on_full_default_cycle(self):
        """No --steps flag → full cycle, which includes collect, so the
        doctor pre-flight runs unchanged."""
        rc, _, _, doctor_calls, _, _ = _run_main(['--date', '2026-05-19'])
        self.assertEqual(rc, 0)
        self.assertEqual(len(doctor_calls), 1)


class TestStepFailurePropagatesExitCode(unittest.TestCase):
    """A step failure inside a subset still returns rc=1 like the full
    cycle. Verifies the return-not-exit refactor preserved exit-code
    semantics."""

    def test_step_failure_returns_1(self):
        rc, _, _, _, _, _ = _run_main(
            ['--date', '2026-05-19', '--steps', 'signals'],
            step_rc=1,
        )
        self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()
