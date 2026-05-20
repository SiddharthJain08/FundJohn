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

const { skipForSubset, strictMode, runSubprocess } = require('./daily_cycle_helpers');
const { resolveScript }                            = require('../../execution/resolve_script');
const pipelineLog                                  = require('../../execution/pipeline_logging');
const traceBus                                     = require('../traceBus');

function makeStepNode(STEP) {
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
    const { argv, timeoutSec } = resolveScript(STEP, state.runDate, env);
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
