"""tests/test_redeploy_pipeline.py

Phase 2 of the regime-redeploy-not-liquidate plan: coverage for
scripts/redeploy_pipeline.py. Mirrors the importlib-from-path loading
pattern used in tests/test_intraday_detector.py since the script lives
outside any importable package.

Every external dependency is mocked:
  - `_redis()` returns a MagicMock (cooldown/sentinel control)
  - `_alpaca_clock_is_rth()` returns the (ok, is_open) tuple to test
  - `subprocess.run` is patched to simulate the orchestrator's exit code
  - `_post_webhook` is patched to record calls

Run:
    pytest tests/test_redeploy_pipeline.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    'redeploy_pipeline',
    ROOT / 'scripts' / 'redeploy_pipeline.py',
)
redeploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redeploy)


# ── Harness ──────────────────────────────────────────────────────────────────

def _fake_redis_clear():
    """Redis client with no cooldown / sentinel keys set."""
    r = MagicMock()
    r.get.return_value = None
    r.ttl.return_value = 3600
    return r


def _fake_redis_cooldown():
    r = MagicMock()
    # cooldown_key returns truthy, sentinel returns None
    def _get(key):
        if 'cooldown' in key:
            return '1'
        return None
    r.get.side_effect = _get
    r.ttl.return_value = 1800
    return r


def _fake_redis_sentinel():
    r = MagicMock()
    def _get(key):
        if 'fired' in key:
            return '1'
        return None
    r.get.side_effect = _get
    r.ttl.return_value = 86400
    return r


def _run_main(argv, *, redis_client, clock_ok=True, clock_is_open=True,
              orchestrator_rc=0, ext_hours_env=False,
              postgres_uri='postgresql://x', capsys=None):
    """Execute redeploy.main(argv) under full mock harness.

    Returns (exit_code, action_payload_dict_or_None, popen_calls,
             webhook_calls).
    """
    popen_calls: list[dict] = []
    webhook_calls: list[tuple[str, str]] = []

    def _fake_run(cmd, **kwargs):
        # Called for the orchestrator spawn AND the alpaca clock probe.
        # Tell them apart by the first arg.
        popen_calls.append({'cmd': list(cmd),
                            'kwargs': kwargs,
                            'env': dict(kwargs.get('env') or {})})
        result = MagicMock()
        # alpaca clock CLI probe
        if cmd and len(cmd) >= 2 and 'clock' in cmd[1:]:
            result.returncode = 0 if clock_ok else 1
            result.stdout = (
                '{"is_open": true}' if clock_is_open
                else '{"is_open": false}'
            )
            result.stderr = ''
        else:
            # orchestrator spawn
            result.returncode = orchestrator_rc
            result.stdout = ''
            result.stderr = ''
        return result

    def _fake_webhook(channel, msg):
        webhook_calls.append((channel, msg))
        return True

    env_overrides = {'POSTGRES_URI': postgres_uri}
    if ext_hours_env:
        env_overrides['OPENCLAW_REDEPLOY_EXTENDED_HOURS'] = '1'
    else:
        # ensure the env doesn't leak from a previous test
        env_overrides.pop('OPENCLAW_REDEPLOY_EXTENDED_HOURS', None)
        os.environ.pop('OPENCLAW_REDEPLOY_EXTENDED_HOURS', None)

    rc = None
    with patch.dict('os.environ', env_overrides, clear=False), \
         patch.object(redeploy, '_redis', return_value=redis_client), \
         patch.object(redeploy, '_post_webhook', side_effect=_fake_webhook), \
         patch.object(redeploy.subprocess, 'run', side_effect=_fake_run):
        rc = redeploy.main(argv)

    # Parse last JSON-line action payload from stdout (capsys).
    action = None
    if capsys is not None:
        captured = capsys.readouterr()
        for line in reversed(captured.out.splitlines()):
            line = line.strip()
            if line.startswith('{'):
                import json
                try:
                    action = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    return rc, action, popen_calls, webhook_calls


def _orchestrator_calls(popen_calls):
    """Return only the orchestrator subprocess.run invocations (filter
    out the alpaca clock probe)."""
    return [c for c in popen_calls
            if not any('clock' == arg for arg in c['cmd'][1:])]


# ── Tests ────────────────────────────────────────────────────────────────────

class TestBlockedByCooldown:
    def test_blocked_by_cooldown(self, capsys):
        rc, action, popen_calls, webhook_calls = _run_main(
            ['--reason', 'INTRADAY_HMM_LOW_VOL_HIGH_VOL',
             '--date', '2026-05-19'],
            redis_client=_fake_redis_cooldown(),
            capsys=capsys,
        )
        assert rc == 0
        assert action is not None
        assert action['action'] == 'blocked'
        assert action['reason'] == 'cooldown_active'
        # No orchestrator spawn, no webhook post
        assert _orchestrator_calls(popen_calls) == []
        assert webhook_calls == []


class TestBlockedBySentinel:
    def test_blocked_by_sentinel(self, capsys):
        rc, action, popen_calls, webhook_calls = _run_main(
            ['--reason', 'INTRADAY_HMM_LOW_VOL_HIGH_VOL',
             '--date', '2026-05-19'],
            redis_client=_fake_redis_sentinel(),
            capsys=capsys,
        )
        assert rc == 0
        assert action['action'] == 'blocked'
        assert action['reason'] == 'same_transition_already_fired'
        assert _orchestrator_calls(popen_calls) == []


class TestExtendedHoursGate:
    def test_blocked_by_extended_hours_disabled(self, capsys):
        """Outside RTH AND env var not set → block."""
        rc, action, popen_calls, _ = _run_main(
            ['--reason', 'INTRADAY_HMM_LOW_VOL_HIGH_VOL',
             '--date', '2026-05-19'],
            redis_client=_fake_redis_clear(),
            clock_ok=True, clock_is_open=False,
            ext_hours_env=False,
            capsys=capsys,
        )
        assert rc == 0
        assert action['action'] == 'blocked'
        assert action['reason'] == 'extended_hours_disabled_until_phase3'
        assert _orchestrator_calls(popen_calls) == []

    def test_extended_hours_enabled_passes_through(self, capsys):
        """Outside RTH BUT env var set → orchestrator IS spawned."""
        rc, action, popen_calls, webhook_calls = _run_main(
            ['--reason', 'INTRADAY_HMM_LOW_VOL_HIGH_VOL',
             '--date', '2026-05-19'],
            redis_client=_fake_redis_clear(),
            clock_ok=True, clock_is_open=False,
            ext_hours_env=True,
            orchestrator_rc=0,
            capsys=capsys,
        )
        assert rc == 0
        assert action['action'] == 'completed'
        spawns = _orchestrator_calls(popen_calls)
        assert len(spawns) == 1
        # Orchestrator invocation must pass --steps + --reason + --date
        cmd = spawns[0]['cmd']
        assert '--steps' in cmd
        idx = cmd.index('--steps')
        assert cmd[idx + 1] == 'signals,handoff,trade,alpaca,reconcile'
        assert '--reason' in cmd
        assert 'INTRADAY_HMM_LOW_VOL_HIGH_VOL' in cmd
        assert '--date' in cmd
        assert '2026-05-19' in cmd
        assert webhook_calls and webhook_calls[0][0] == 'intraday-regime'


class TestSuccessSetsKeys:
    def test_success_sets_cooldown_and_sentinel(self, capsys):
        r = _fake_redis_clear()
        rc, action, popen_calls, webhook_calls = _run_main(
            ['--reason', 'INTRADAY_HMM_LOW_VOL_HIGH_VOL',
             '--date', '2026-05-19'],
            redis_client=r,
            clock_ok=True, clock_is_open=True,
            orchestrator_rc=0,
            capsys=capsys,
        )
        assert rc == 0
        assert action['action'] == 'completed'
        assert action['exit_code'] == 0

        # Inspect r.set calls for cooldown + sentinel keys
        set_keys = {call.args[0]: call for call in r.set.call_args_list}
        assert 'redeploy:cooldown:2026-05-19' in set_keys
        assert any(k.startswith('redeploy:fired:2026-05-19:')
                   for k in set_keys)

        # TTLs (passed as ex=N kwarg)
        cooldown_call = set_keys['redeploy:cooldown:2026-05-19']
        assert cooldown_call.kwargs.get('ex') == 3600

        sentinel_key_name = [k for k in set_keys
                             if k.startswith('redeploy:fired:')][0]
        sentinel_call = set_keys[sentinel_key_name]
        assert sentinel_call.kwargs.get('ex') == 86400

        # Success webhook posted
        assert webhook_calls
        channel, msg = webhook_calls[0]
        assert channel == 'intraday-regime'
        assert ':rocket:' in msg
        assert 'INTRADAY_HMM_LOW_VOL_HIGH_VOL' in msg


class TestForceResizeEnv:
    def test_orchestrator_spawned_with_force_resize(self, capsys):
        """Redeploy must hand OPENCLAW_FORCE_RESIZE=1 to the orchestrator
        so the sized handoff's idempotency guard doesn't refuse to
        overwrite after the morning cycle has already submitted orders."""
        rc, action, popen_calls, _ = _run_main(
            ['--reason', 'INTRADAY_HMM_LOW_VOL_HIGH_VOL',
             '--date', '2026-05-19'],
            redis_client=_fake_redis_clear(),
            clock_ok=True, clock_is_open=True,
            orchestrator_rc=0,
            capsys=capsys,
        )
        assert rc == 0
        orch_calls = _orchestrator_calls(popen_calls)
        assert orch_calls, 'orchestrator should have been spawned'
        assert orch_calls[0]['env'].get('OPENCLAW_FORCE_RESIZE') == '1', \
            f"FORCE_RESIZE missing from env: {orch_calls[0]['env']}"


class TestFailurePath:
    def test_failure_no_keys_set(self, capsys):
        r = _fake_redis_clear()
        rc, action, popen_calls, webhook_calls = _run_main(
            ['--reason', 'INTRADAY_HMM_LOW_VOL_HIGH_VOL',
             '--date', '2026-05-19'],
            redis_client=r,
            clock_ok=True, clock_is_open=True,
            orchestrator_rc=1,
            capsys=capsys,
        )
        assert rc == 1
        assert action['action'] == 'failed'
        assert action['exit_code'] == 1

        # Cooldown / sentinel keys must NOT be set on failure
        set_keys = [c.args[0] for c in r.set.call_args_list]
        assert not any(k.startswith('redeploy:cooldown:')
                       for k in set_keys), set_keys
        assert not any(k.startswith('redeploy:fired:')
                       for k in set_keys), set_keys

        # Failure webhook posted
        assert webhook_calls
        channel, msg = webhook_calls[0]
        assert channel == 'intraday-regime'
        assert ':warning:' in msg
        assert 'exit_code=1' in msg
        assert 'logs/redeploy_pipeline_2026-05-19.log' in msg

    def test_orchestrator_fatal_propagates(self, capsys):
        rc, action, _, _ = _run_main(
            ['--reason', 'INTRADAY_HMM_LOW_VOL_HIGH_VOL',
             '--date', '2026-05-19'],
            redis_client=_fake_redis_clear(),
            clock_ok=True, clock_is_open=True,
            orchestrator_rc=2,
            capsys=capsys,
        )
        assert rc == 2
        assert action['action'] == 'failed'
        assert action['exit_code'] == 2


class TestReasonRequired:
    def test_reason_required(self):
        with pytest.raises(SystemExit) as exc_info:
            redeploy.main(['--date', '2026-05-19'])
        # argparse exits 2 when a required arg is missing
        assert exc_info.value.code == 2


class TestReasonClamped:
    def test_reason_clamped(self, capsys):
        long_reason = 'A' * 200
        r = _fake_redis_clear()
        rc, action, popen_calls, webhook_calls = _run_main(
            ['--reason', long_reason, '--date', '2026-05-19'],
            redis_client=r,
            clock_ok=True, clock_is_open=True,
            orchestrator_rc=0,
            capsys=capsys,
        )
        assert rc == 0

        # Sentinel key reason segment must be exactly 80 chars
        sentinel_keys = [c.args[0] for c in r.set.call_args_list
                         if c.args[0].startswith('redeploy:fired:')]
        assert len(sentinel_keys) == 1
        # 'redeploy:fired:2026-05-19:' + 80 A's
        assert sentinel_keys[0] == f'redeploy:fired:2026-05-19:{"A" * 80}'

        # Webhook message contains 80-char form but not 100-char form
        assert webhook_calls
        msg = webhook_calls[0][1]
        assert 'A' * 80 in msg
        assert 'A' * 100 not in msg
