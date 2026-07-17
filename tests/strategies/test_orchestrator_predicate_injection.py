"""
SP-2 Phase D Task 3 — test _validateInferredFilter from research-orchestrator.js.

Drives the REAL exported function via `node -e` to avoid re-implementing logic
in the test. Exercises the gate, whitelist, null, and invalid-name cases.

Requirements:
  - OPENCLAW_DIR must resolve to the worktree root (default: path.join(__dirname, '../../..'))
  - python3 + src/strategies/universe_default.py must be importable from that dir
  - No POSTGRES_URI required (validator is pure / synchronous, no DB calls)
"""
import os
import subprocess
import sys

# Resolve the worktree root (parent of tests/)
WORKTREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Single-line JS avoids Node 22 TypeScript-eval multi-line issues.
# When running `node -e CODE ARG`, process.argv == ['/path/node', ARG].
NODE_SNIPPET = (
    'const Orch = require("./src/agent/research/research-orchestrator");'
    ' const fn = Orch._validateInferredFilter;'
    ' const arg = process.argv[1];'
    ' const val = arg === "__NULL__" ? null : arg;'
    ' console.log(JSON.stringify(fn(val)));'
)


def call_validator(predicate_arg, *, gate_on):
    """Run _validateInferredFilter(predicate_arg) in node, return parsed result."""
    env = os.environ.copy()
    env['OPENCLAW_DIR'] = WORKTREE
    if gate_on:
        env['OPENCLAW_PHASE_D_PREDICATE_AT_MINT'] = '1'
    else:
        env.pop('OPENCLAW_PHASE_D_PREDICATE_AT_MINT', None)

    # Pass __NULL__ as sentinel because node -e argv[2] can't be real null
    arg = '__NULL__' if predicate_arg is None else predicate_arg

    proc = subprocess.run(
        ['node', '-e', NODE_SNIPPET, arg],
        capture_output=True, text=True, cwd=WORKTREE, env=env, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f'node exited {proc.returncode}: stderr={proc.stderr[:400]}'
        )
    import json
    return json.loads(proc.stdout.strip())


def test_gate_on_valid_name_returns_name():
    """Gate ON + valid predicate name → returns the name."""
    result = call_validator('sp500', gate_on=True)
    assert result == 'sp500', f'expected "sp500", got {result!r}'


def test_gate_on_invalid_name_returns_null():
    """Gate ON + unknown predicate name → falls back to null."""
    result = call_validator('NOT_A_PREDICATE', gate_on=True)
    assert result is None, f'expected null, got {result!r}'


def test_gate_on_null_input_returns_null():
    """Gate ON + null input → null (nothing to validate)."""
    result = call_validator(None, gate_on=True)
    assert result is None, f'expected null for null input, got {result!r}'


def test_gate_off_valid_name_returns_null():
    """Gate OFF + valid name → null (feature is disabled)."""
    result = call_validator('sp500', gate_on=False)
    assert result is None, f'expected null when gate is OFF, got {result!r}'


def test_all_12_candidates_accepted():
    """All 12 CANDIDATE_PREDICATES names are accepted when gate is ON."""
    # Import the real list from Python to ensure the test stays in sync
    sys.path.insert(0, WORKTREE)
    from src.strategies.universe_default import CANDIDATE_PREDICATES
    for name in CANDIDATE_PREDICATES:
        result = call_validator(name, gate_on=True)
        assert result == name, f'expected {name!r}, got {result!r}'


if __name__ == '__main__':
    tests = [
        test_gate_on_valid_name_returns_name,
        test_gate_on_invalid_name_returns_null,
        test_gate_on_null_input_returns_null,
        test_gate_off_valid_name_returns_null,
        test_all_12_candidates_accepted,
    ]
    failures = []
    for t in tests:
        try:
            t()
            print(f'  PASS  {t.__name__}')
        except Exception as e:
            print(f'  FAIL  {t.__name__}: {e}')
            failures.append(t.__name__)
    if failures:
        print(f'\n{len(failures)} test(s) FAILED: {failures}')
        sys.exit(1)
    print(f'\nAll {len(tests)} tests passed.')
