"""tests/test_redeploy_main_flag_on.py

Flag-ON end-to-end tests for redeploy_pipeline.main().

Closes the reviewer gap: exercises the flag-ON code path in main()
(the `if _pf.prefetch_enabled(): ... finally: _pf.release_inflight(...)` block)
with genuine control-flow assertions.

Three scenarios under OPENCLAW_INTRADAY_15MIN_PREFETCH=1:
  1. abort path  — _data_ready_gate returns 'abort'; no orchestrator spawn.
  2. proceed path — gate returns 'proceed', orchestrator succeeds; sentinel set,
                    cooldown NOT set, lock released.
  3. fail path   — gate returns 'proceed', orchestrator returns 1;
                   sentinel NOT set, lock released.
"""
from __future__ import annotations
import json
import os

import pytest

import scripts.redeploy_pipeline as rd
import src.execution.intraday_prefetch as _pf

# ── FakeRedis (supports pre-seeded kv and nx-semantics) ─────────────────────

class FakeRedis:
    def __init__(self, kv=None): self.kv = dict(kv) if kv else {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    def delete(self, k): self.kv.pop(k, None)
    def ttl(self, k): return 60


# ── Shared harness ────────────────────────────────────────────────────────────

def _run_main_flag_on(argv, *, monkeypatch, redis_kv=None,
                      gate_result='proceed', orchestrator_rc=0, capsys):
    """Run rd.main(argv) with the flag ON and all external boundaries mocked.

    Returns (exit_code, action_payload_or_None).
    """
    # Pre-seed in-flight lock so we can later assert it was released.
    kv = dict(redis_kv or {})
    kv[_pf.INFLIGHT_KEY] = '1'   # simulate the detector having acquired it
    r = FakeRedis(kv=kv)

    # All calls to _redis() must return the SAME instance (main() calls it
    # again inside the finally block for release_inflight).
    monkeypatch.setattr(rd, '_redis', lambda: r)

    # gate mocked at the rd module level
    monkeypatch.setattr(rd, '_data_ready_gate', lambda date: gate_result)

    # clock: always RTH so the extended-hours block passes through
    monkeypatch.setattr(rd, '_alpaca_clock_is_rth', lambda: (True, True))

    # track _spawn_orchestrator calls
    spawn_calls = []
    def _fake_spawn(reason, run_date, dry_run):
        spawn_calls.append({'reason': reason, 'run_date': run_date})
        return orchestrator_rc
    monkeypatch.setattr(rd, '_spawn_orchestrator', _fake_spawn)

    # suppress network calls in _post_webhook
    monkeypatch.setattr(rd, '_post_webhook', lambda ch, msg: True)

    monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://x')

    rc = rd.main(argv)

    # parse the action JSON printed to stdout
    action = None
    captured = capsys.readouterr()
    for line in reversed(captured.out.splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                action = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    return rc, action, r, spawn_calls


# ── Tests ─────────────────────────────────────────────────────────────────────

ARGV = ['--reason', 'TEST_T_HV', '--date', '2026-06-09']


class TestAbortPath:
    """_data_ready_gate → 'abort': no spawn, in-flight lock released."""

    def test_abort_rc_is_0(self, monkeypatch, capsys):
        rc, _, _, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='abort', capsys=capsys)
        assert rc == 0

    def test_abort_action_json(self, monkeypatch, capsys):
        _, action, _, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='abort', capsys=capsys)
        assert action is not None, 'expected an action JSON line on stdout'
        assert action['action'] == 'aborted', (
            f"expected action='aborted', got '{action.get('action')}' — "
            f"a different gate may have fired (extended-hours, cooldown, etc.)"
        )

    def test_abort_spawn_not_called(self, monkeypatch, capsys):
        _, _, _, spawn_calls = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='abort', capsys=capsys)
        assert spawn_calls == [], f'orchestrator should not have been called; got: {spawn_calls}'

    def test_abort_lock_released(self, monkeypatch, capsys):
        _, _, r, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='abort', capsys=capsys)
        assert r.kv.get(_pf.INFLIGHT_KEY) is None, (
            f"in-flight lock '{_pf.INFLIGHT_KEY}' should have been deleted; "
            f"kv={r.kv}"
        )


class TestProceedPath:
    """gate → 'proceed', orchestrator succeeds: sentinel SET, cooldown NOT SET, lock released."""

    def test_proceed_rc_is_0(self, monkeypatch, capsys):
        rc, _, _, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=0, capsys=capsys)
        assert rc == 0

    def test_proceed_action_json(self, monkeypatch, capsys):
        _, action, _, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=0, capsys=capsys)
        assert action is not None
        assert action['action'] == 'completed', (
            f"expected action='completed', got '{action.get('action')}'"
        )

    def test_proceed_spawn_was_called(self, monkeypatch, capsys):
        _, _, _, spawn_calls = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=0, capsys=capsys)
        assert len(spawn_calls) == 1, f'expected 1 orchestrator call, got {spawn_calls}'
        assert spawn_calls[0]['reason'] == 'TEST_T_HV'
        assert spawn_calls[0]['run_date'] == '2026-06-09'

    def test_proceed_sentinel_is_set(self, monkeypatch, capsys):
        _, _, r, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=0, capsys=capsys)
        sentinel_key = 'redeploy:fired:2026-06-09:TEST_T_HV'
        assert r.kv.get(sentinel_key) == '1', (
            f"sentinel key '{sentinel_key}' not set; kv={r.kv}"
        )

    def test_proceed_cooldown_not_set(self, monkeypatch, capsys):
        """Flag ON: cooldown dropped — redeploy:cooldown must NOT be set."""
        _, _, r, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=0, capsys=capsys)
        cooldown_key = f'redeploy:cooldown:2026-06-09'
        assert r.kv.get(cooldown_key) is None, (
            f"cooldown key '{cooldown_key}' was set but should be absent under "
            f"flag-ON (cooldown dropped); kv={r.kv}"
        )

    def test_proceed_lock_released(self, monkeypatch, capsys):
        _, _, r, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=0, capsys=capsys)
        assert r.kv.get(_pf.INFLIGHT_KEY) is None, (
            f"in-flight lock '{_pf.INFLIGHT_KEY}' should have been deleted; kv={r.kv}"
        )


class TestProceedOrchestratorFails:
    """gate → 'proceed', orchestrator returns 1: rc==1, sentinel NOT set, lock released."""

    def test_fail_rc_is_1(self, monkeypatch, capsys):
        rc, _, _, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=1, capsys=capsys)
        assert rc == 1

    def test_fail_action_json(self, monkeypatch, capsys):
        _, action, _, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=1, capsys=capsys)
        assert action is not None
        assert action['action'] == 'failed'

    def test_fail_sentinel_not_set(self, monkeypatch, capsys):
        _, _, r, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=1, capsys=capsys)
        sentinel_key = 'redeploy:fired:2026-06-09:TEST_T_HV'
        assert r.kv.get(sentinel_key) is None, (
            f"sentinel should NOT be set on failure; kv={r.kv}"
        )

    def test_fail_lock_released(self, monkeypatch, capsys):
        _, _, r, _ = _run_main_flag_on(
            ARGV, monkeypatch=monkeypatch, gate_result='proceed',
            orchestrator_rc=1, capsys=capsys)
        assert r.kv.get(_pf.INFLIGHT_KEY) is None, (
            f"in-flight lock should be released even on orchestrator failure; kv={r.kv}"
        )
