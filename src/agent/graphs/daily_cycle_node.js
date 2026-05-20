// src/agent/graphs/daily_cycle_node.js
/**
 * Node-template factory for the 11 daily-cycle steps.
 *
 *   const collectNode = makeStepNode('collect');
 *   const tradeNode   = makeStepNode('trade');
 *
 * Each generated nodeFn(state, config) follows the spec §3.4 pattern:
 *   1. honor requestedSteps subset filter
 *   2. emit traceBus events (start | ok | warn | err | skipped)
 *   3. post Discord notifications via pipeline_logging.js
 *   4. subprocess-exec the step's script via resolve_script.js
 *   5. map rc → status (rc=0 ok | rc=1 warn-or-throw | rc≥2 throw)
 */
'use strict';

const path = require('node:path');

const { skipForSubset, strictMode, runSubprocess } = require('./daily_cycle_helpers');
const { resolveScript }                            = require('../../execution/resolve_script');
const pipelineLog                                  = require('../../execution/pipeline_logging');
const traceBus                                     = require('../traceBus');

// engine.py, alpaca_executor.py etc. import `strategies.xxx` / `database.xxx`
// as top-level packages — those live under src/, so PYTHONPATH needs both
// ROOT (for `src.xxx` imports) and ROOT/src (for bare-package imports).
// Mirrors pipeline_orchestrator.py:710.
const ROOT = path.resolve(__dirname, '..', '..', '..');
function _pythonpath(existing) {
  const parts = [ROOT, path.join(ROOT, 'src')];
  if (existing) parts.push(existing);
  return parts.join(path.delimiter);
}

function makeStepNode(STEP, scriptName) {
  // scriptName is optional — defaults to STEP for backward compat with simple cases
  // (signals/handoff/trade/etc. where step name == script base name). For mapped
  // steps (collect → run_collector_once, sentiment → run_sentiment_step, etc.)
  // the caller passes the script name explicitly.
  const SCRIPT = scriptName || STEP;
  return async function stepNode(state, _config) {
    const runId = state.runId;

    if (skipForSubset(STEP, state)) {
      traceBus.push({ runId, node: STEP, status: 'skipped', ts: Date.now() });
      return {};
    }

    const startedAt = Date.now();
    traceBus.push({ runId, node: STEP, status: 'start', ts: startedAt });
    await pipelineLog.feedStart(STEP, state.runDate, state.reason);

    const env = { ...process.env, ...(state.env || {}) };
    // Inject PYTHONPATH so Python steps can `from strategies.X import ...` etc.
    env.PYTHONPATH = _pythonpath(env.PYTHONPATH);
    const { argv, timeoutSec } = resolveScript(SCRIPT, state.runDate, env);
    const { rc, stderrTail, durationMs, timedOut } = await runSubprocess(argv, { timeoutSec, env });

    const completion = {
      step:       STEP,
      rc,
      durationMs,
      startedAt,
      finishedAt: Date.now(),
      timedOut:   timedOut || false,
      status:     'failed', // overwritten below
    };

    if (rc === 0) {
      completion.status = 'ok';
      traceBus.push({ runId, node: STEP, status: 'ok', ts: Date.now(), duration: durationMs });
      await pipelineLog.feedEnd(STEP, 'ok', state.runDate, durationMs);
      return { completedSteps: [...(state.completedSteps || []), completion] };
    }

    if (rc === 1 && !strictMode(env)) {
      completion.status = 'warn';
      traceBus.push({ runId, node: STEP, status: 'warn', ts: Date.now(), duration: durationMs });
      await pipelineLog.feedEnd(STEP, 'warn', state.runDate, durationMs);
      return { completedSteps: [...(state.completedSteps || []), completion] };
    }

    // rc ≥ 2 always aborts; rc=1 in strict mode also aborts
    traceBus.push({ runId, node: STEP, status: 'err', ts: Date.now(), rc, stderrTail });
    await pipelineLog.notifyFailure(STEP, state.runDate, rc, stderrTail);

    const err = new Error(`step ${STEP} exited rc=${rc}`);
    err.step       = STEP;
    err.rc         = rc;
    err.stderrTail = stderrTail;
    err.timedOut   = timedOut || false;
    throw err;
  };
}

module.exports = { makeStepNode };
