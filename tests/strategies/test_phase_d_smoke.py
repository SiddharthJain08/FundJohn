"""
SP-2 Phase D Task 6 — fast smoke tests for prompt sections, lifecycle AST
detection, and the orchestrator _validateInferredFilter whitelist.

All three tests are deterministic and require no DB / network access.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# Resolve the worktree root (parent of tests/)
WORKTREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_both_prompts_have_phase_d_sections():
    ph = Path(WORKTREE, 'src/agent/prompts/subagents/paperhunter.md').read_text()
    sc = Path(WORKTREE, 'src/agent/prompts/subagents/strategycoder.md').read_text()
    assert '## Step 5 — Infer universe slice' in ph
    assert 'inferred_universe_filter' in ph
    assert 'Universe predicate' in sc
    assert 'as universe_filter' in sc


def test_lifecycle_module_detect_smoke(tmp_path):
    sys.path.insert(0, WORKTREE)
    from src.strategies.lifecycle import _detect_module_predicate
    f = tmp_path / 's.py'
    f.write_text('from src.strategies.universe_default import sp500 as universe_filter\n')
    assert _detect_module_predicate(f) == 'sp500'


# Single-line JS snippet — mirrors test_orchestrator_predicate_injection.py pattern.
# process.argv[1] carries the predicate name (or sentinel __NULL__ for null).
_NODE_SNIPPET = (
    'const Orch = require("./src/agent/research/research-orchestrator");'
    ' const fn = Orch._validateInferredFilter;'
    ' const arg = process.argv[1];'
    ' const val = arg === "__NULL__" ? null : arg;'
    ' console.log(JSON.stringify(fn(val)));'
)


def _run_validator(name, *, gate_on):
    env = os.environ.copy()
    env['OPENCLAW_DIR'] = WORKTREE
    if gate_on:
        env['OPENCLAW_PHASE_D_PREDICATE_AT_MINT'] = '1'
    else:
        env.pop('OPENCLAW_PHASE_D_PREDICATE_AT_MINT', None)

    arg = '__NULL__' if name is None else name
    proc = subprocess.run(
        ['node', '-e', _NODE_SNIPPET, arg],
        capture_output=True, text=True, cwd=WORKTREE, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[:400]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_orchestrator_validator_whitelists():
    # Gate ON: valid name → returned as-is; unknown name → null.
    assert _run_validator('sp500', gate_on=True) == 'sp500'
    assert _run_validator('NOT_REAL', gate_on=True) is None
