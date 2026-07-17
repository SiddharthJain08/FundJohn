"""SP-4: _validateInferredClass — gate ON/OFF, whitelist, fallback.
Run: pytest tests/test_orchestrator_instrument_class_injection.py -v
"""
import json
import os
import subprocess
from pathlib import Path

WORKTREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NODE_SNIPPET = (
    'const Orch = require("./src/agent/research/research-orchestrator");'
    ' const fn = Orch._validateInferredClass;'
    ' const arg = process.argv[1];'
    ' const val = arg === "__NULL__" ? null : arg;'
    ' console.log(JSON.stringify(fn(val)));'
)


def _call(arg, *, gate_on):
    env = os.environ.copy()
    env['OPENCLAW_DIR'] = WORKTREE
    if gate_on:
        env['OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT'] = '1'
    else:
        env.pop('OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT', None)
    proc = subprocess.run(
        ['node', '-e', NODE_SNIPPET, arg],
        capture_output=True, text=True, cwd=WORKTREE, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_gate_off_always_equity():
    assert _call('option', gate_on=False) == 'equity'
    assert _call('crypto', gate_on=False) == 'equity'


def test_null_is_equity():
    assert _call('__NULL__', gate_on=True) == 'equity'


def test_valid_classes_pass_when_gate_on():
    for cls in ('equity', 'option', 'etp', 'crypto', 'futures'):
        assert _call(cls, gate_on=True) == cls


def test_unknown_falls_back_to_equity():
    assert _call('banana', gate_on=True) == 'equity'
