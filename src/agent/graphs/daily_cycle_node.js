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
    const { rc, stdout, stderrTail, durationMs, timedOut } = await runSubprocess(argv, { timeoutSec, env });

    // Persist the step's stdout AND stderr tails on EVERY completion (success
    // included). rc=0 zero-order days were un-diagnosable twice (2026-06-02/03):
    // the sizer's own log line naming the cause ("dropped N tickers below
    // min_cum_sharpe=4.00 (kept=0)") was captured above and then discarded —
    // only aborts persisted anything (stderr, below). Same pattern, stderr
    // edition (2026-07-02): the sizer's rc=0 diagnostics (corr_cumsharpe.live /
    // breadth_weight / clamp) print to stderr whose only other sink is a
    // self-expiring Discord post (pipeline_config corr_cumsharpe_log_until).
    // Tail-bounded so chatty steps (engine.py) can't bloat the file. Failure
    // here must never break the step itself.
    try {
      const fs = require('node:fs');
      const logDir = path.join(ROOT, 'logs');
      try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
      const stepsLog = path.join(logDir, `daily_cycle_steps_${state.runDate}.log`);
      const header = `\n=== ${new Date().toISOString()} step=${STEP} rc=${rc} durationMs=${durationMs} timedOut=${timedOut || false} runId=${runId} ===\n`;
      const tail = (stdout || '').slice(-4000);
      const errTail = (stderrTail || '').slice(-4000);
      fs.appendFileSync(stepsLog,
        header + (tail || '(stdout empty)') + '\n'
        + (errTail ? `--- stderr tail ---\n${errTail}\n` : ''));
    } catch (e) {
      console.warn(`[daily_cycle_node] stdout persistence failed for ${STEP}: ${e.message}`);
    }

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

    // Persist full stderr so the abort root cause survives past the
    // Discord notification (which truncates to 400 chars). The
    // in-conversation incident 2026-05-22 LangGraph trade abort had no
    // capturable stderr anywhere because nothing wrote it to disk.
    try {
      const fs = require('node:fs');
      const path = require('node:path');
      const logDir = path.join(ROOT, 'logs');
      try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
      const logPath = path.join(logDir, `daily_cycle_aborts_${state.runDate}.log`);
      const header = `\n=== ${new Date().toISOString()} step=${STEP} rc=${rc} timedOut=${timedOut || false} runId=${runId} ===\n`;
      fs.appendFileSync(logPath, header + (stderrTail || '(stderr empty)') + '\n');
    } catch (e) {
      console.warn(`[daily_cycle_node] stderr persistence failed for ${STEP}: ${e.message}`);
    }

    // Sentiment is a gap-filler: signals runs fine without it (the step is
    // env-gated OFF by default), so a slow or failed scrape must never cost
    // the next session's COMPUTED set. 2026-07-22: the universe-wide Alpaca
    // news ingest blew the 600s step budget (rc=124) and aborted the 16:15
    // EOD compute before `signals` — recovered by hand. The Discord failure
    // alert + stderr persistence above still fire; only the abort is skipped.
    if (STEP === 'sentiment') {
      completion.status = 'warn';
      await pipelineLog.feedEnd(STEP, 'warn', state.runDate, durationMs);
      return { completedSteps: [...(state.completedSteps || []), completion] };
    }

    const err = new Error(`step ${STEP} exited rc=${rc}`);
    err.step       = STEP;
    err.rc         = rc;
    err.stderrTail = stderrTail;
    err.timedOut   = timedOut || false;
    throw err;
  };
}

module.exports = { makeStepNode };
