"""SP-4: _optionUnderlyingSupported — only Phase-0 index/ETF underlyings pass.
Run: pytest tests/test_orchestrator_option_envelope.py -v
"""
import json
import os
import subprocess

WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NODE_SNIPPET = (
    'const Orch = require("./src/agent/research/research-orchestrator");'
    ' const fn = Orch._optionUnderlyingSupported;'
    ' const arg = process.argv[1];'
    ' const val = arg === "__NULL__" ? null : arg;'
    ' console.log(JSON.stringify(fn(val)));'
)


def _call(arg):
    env = os.environ.copy()
    env['OPENCLAW_DIR'] = WORKTREE
    proc = subprocess.run(
        ['node', '-e', NODE_SNIPPET, arg],
        capture_output=True, text=True, cwd=WORKTREE, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_supported_index_etf_pass():
    assert _call('SPY') is True
    assert _call('QQQ') is True
    assert _call('IWM') is True


def test_single_name_rejected():
    assert _call('AAPL') is False


def test_null_rejected():
    assert _call('__NULL__') is False
