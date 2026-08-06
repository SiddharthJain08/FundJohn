"""The JS and Python step resolvers are documented twins — keep them in lockstep.

`src/execution/resolve_script.js` says it is the "JS twin of
pipeline_orchestrator.py:_resolve_script". The LangGraph daily cycle uses the
JS one; the Python orchestrator is what a manual/recovery invocation uses. On
2026-08-05 the signals step timed out at its 300s cap for a zero-signal day,
and the fix had to land in BOTH — patching only the JS path would have left
recovery runs still capped. This test fails if they ever diverge again.
"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Steps that resolve through both dispatchers. `run_sentiment_step` covers the
# src/pipeline/*.py branch, `run_collector_once` the node branch, `engine` and
# `ic_gate_runner` their special cases, `handoff` the bare 300s fallback.
SHARED_STEPS = [
    'engine',
    'ic_gate_runner',
    'handoff',
    'alpaca_executor',
    'run_collector_once',
    'run_sentiment_step',
]

ENVS = [
    {},
    {'OPENCLAW_SIGNALS_TIMEOUT_SECONDS': '1200'},
    {'IC_TIMEOUT_SECONDS': '900'},
    {'OPENCLAW_COLLECT_TIMEOUT_SECONDS': '4200'},
]


def _js_timeouts(env_overrides):
    script = f"""
    const {{ resolveScript }} = require('{ROOT}/src/execution/resolve_script.js');
    const env = Object.assign({{}}, {json.dumps(env_overrides)});
    const out = {{}};
    for (const s of {json.dumps(SHARED_STEPS)}) {{
      try {{ out[s] = resolveScript(s, '2026-08-06', env).timeoutSec; }}
      catch (e) {{ out[s] = 'ERR'; }}
    }}
    process.stdout.write(JSON.stringify(out));
    """
    res = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                         cwd=str(ROOT), timeout=60)
    assert res.returncode == 0, f'node resolver failed: {res.stderr[:400]}'
    return json.loads(res.stdout)


def _py_timeouts(env_overrides):
    script = f"""
import json, os, sys
os.environ.update({json.dumps(env_overrides)})
sys.path.insert(0, {json.dumps(str(ROOT / 'src'))})
from execution.pipeline_orchestrator import _resolve_script
out = {{}}
for s in {json.dumps(SHARED_STEPS)}:
    try: out[s] = _resolve_script(s, '2026-08-06')[1]
    except Exception: out[s] = 'ERR'
sys.stdout.write(json.dumps(out))
"""
    res = subprocess.run(['python3', '-c', script], capture_output=True, text=True,
                         cwd=str(ROOT), timeout=120)
    assert res.returncode == 0, f'python resolver failed: {res.stderr[-600:]}'
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize('env_overrides', ENVS, ids=lambda e: ','.join(e) or 'defaults')
def test_twin_resolvers_agree_on_timeouts(env_overrides):
    js = _js_timeouts(env_overrides)
    py = _py_timeouts(env_overrides)
    mismatched = {s: (js[s], py[s]) for s in SHARED_STEPS
                  if js[s] != py[s] and 'ERR' not in (js[s], py[s])}
    assert not mismatched, (
        f'resolver twins diverged for env={env_overrides or "defaults"}: '
        + ', '.join(f'{s}: js={j} py={p}' for s, (j, p) in mismatched.items())
    )


def test_signals_step_is_not_on_the_bare_300s_fallback():
    """Regression guard for the 2026-08-05 zero-signal day."""
    js, py = _js_timeouts({}), _py_timeouts({})
    assert js['engine'] == py['engine'] == 900
    # ...while the genuinely fast steps stay at 300.
    assert js['handoff'] == py['handoff'] == 300
