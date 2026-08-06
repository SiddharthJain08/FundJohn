"""§5 (2026-08-06 remediation spec): the signals step retries exactly once.

The retry lives in BOTH twins — daily_cycle_node.js (the live LangGraph
path) and pipeline_orchestrator.py (manual/recovery runs). On 2026-08-05 a
single abort cost the entire trading day's signals; the only prior recovery
on record was a human. These tests pin (a) source-level parity of the gate
in both twins, and (b) the JS node's behavior end-to-end with a stubbed
subprocess: one retry on abort-worthy rc, no retry on success, no retry on
soft-warn rc=1, kill switch honored, and other steps never retried.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

JS_NODE = ROOT / 'src' / 'agent' / 'graphs' / 'daily_cycle_node.js'
PY_ORCH = ROOT / 'src' / 'execution' / 'pipeline_orchestrator.py'


def test_gate_present_in_both_twins():
    js = JS_NODE.read_text()
    py = PY_ORCH.read_text()
    for src, name in ((js, 'daily_cycle_node.js'), (py, 'pipeline_orchestrator.py')):
        assert 'OPENCLAW_SIGNALS_RETRY' in src, f'{name} lost the retry gate'
    # Default-ON semantics: both twins must treat unset as enabled.
    assert "env.OPENCLAW_SIGNALS_RETRY !== '0'" in js
    assert "os.environ.get('OPENCLAW_SIGNALS_RETRY', '1') != '0'" in py


def _run_node_scenario(step, rcs, env_extra=None):
    """Drive makeStepNode(step) with runSubprocess stubbed to pop `rcs`.

    Returns {'attempts': N, 'outcome': 'ok'|'warn'|'thrown', 'rc': final}.
    """
    scenario = {
        'step': step,
        'rcs': rcs,
        'env': env_extra or {},
    }
    script = """
    const path = require('node:path');
    const scenario = %s;
    // The node's tail-persist appends to the REAL logs/daily_cycle_steps_<date>.log
    // — stub it out or every test run pollutes the production step log
    // (found 2026-08-06: runId=t artifacts in the live log).
    const fs = require('node:fs');
    const _origAppend = fs.appendFileSync;
    fs.appendFileSync = (p, data) => {
      if (String(p).includes('daily_cycle')) return;
      return _origAppend(p, data);
    };
    const helpersPath = path.join(%s, 'src', 'agent', 'graphs', 'daily_cycle_helpers.js');
    const helpers = require(helpersPath);
    let attempts = 0;
    helpers.runSubprocess = async () => {
      const rc = scenario.rcs[Math.min(attempts, scenario.rcs.length - 1)];
      attempts += 1;
      return { rc, stdout: '', stderrTail: '', durationMs: 5, timedOut: false };
    };
    // Silence Discord + traceBus side effects.
    const plPath = path.join(%s, 'src', 'execution', 'pipeline_logging.js');
    const pl = require(plPath);
    for (const k of ['feedStart', 'feedEnd', 'notifyFailure']) pl[k] = async () => {};
    const tbPath = path.join(%s, 'src', 'agent', 'traceBus.js');
    const tb = require(tbPath);
    tb.push = () => {};
    const { makeStepNode } = require(path.join(%s, 'src', 'agent', 'graphs', 'daily_cycle_node.js'));
    const node = makeStepNode(scenario.step, 'engine');
    const state = { runId: 't', runDate: '2026-08-06', completedSteps: [],
                    env: scenario.env, requestedSteps: null, reason: 'test' };
    node(state, {}).then(
      (out) => {
        const c = out.completedSteps[out.completedSteps.length - 1];
        process.stdout.write(JSON.stringify({ attempts, outcome: c.status, rc: c.rc }));
      },
      (err) => {
        process.stdout.write(JSON.stringify({ attempts, outcome: 'thrown', rc: err.rc }));
      });
    """ % (json.dumps(scenario), *([json.dumps(str(ROOT))] * 4))
    res = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                         cwd=str(ROOT), timeout=60)
    assert res.returncode == 0, f'node scenario failed: {res.stderr[:500]}'
    return json.loads(res.stdout)


def test_js_signals_retries_once_then_succeeds():
    out = _run_node_scenario('signals', [137, 0])
    assert out == {'attempts': 2, 'outcome': 'ok', 'rc': 0}


def test_js_signals_retries_once_then_gives_up():
    out = _run_node_scenario('signals', [137, 124])
    assert out['attempts'] == 2, 'retry must be bounded to ONE'
    assert out['outcome'] == 'thrown'
    assert out['rc'] == 124


def test_js_signals_success_never_retries():
    out = _run_node_scenario('signals', [0])
    assert out == {'attempts': 1, 'outcome': 'ok', 'rc': 0}


def test_js_signals_soft_warn_never_retries():
    # rc=1 outside strict mode continues the chain as warn — not abort-worthy.
    out = _run_node_scenario('signals', [1])
    assert out == {'attempts': 1, 'outcome': 'warn', 'rc': 1}


def test_js_kill_switch():
    out = _run_node_scenario('signals', [137, 0],
                             env_extra={'OPENCLAW_SIGNALS_RETRY': '0'})
    assert out['attempts'] == 1
    assert out['outcome'] == 'thrown'


def test_js_other_steps_never_retry():
    out = _run_node_scenario('trade', [137, 0])
    assert out['attempts'] == 1
    assert out['outcome'] == 'thrown'
