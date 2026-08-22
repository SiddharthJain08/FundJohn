// src/agent/graphs/daily-cycle.js
/**
 * Unified daily-cycle LangGraph.
 *
 *   START → collect → sentiment → signals → ic_gate → handoff →
 *   trade → alpaca → reconcile → report → pyportfolioopt_shadow →
 *   health → END
 *
 * One thread per runDate. PostgresSaver persists state after each
 * node (or MemorySaver in tests). traceBus events surface in dashboard
 * SSE. Replaces pipeline_orchestrator.py once OPENCLAW_LANGGRAPH_ORCHESTRATOR=1.
 */
'use strict';

const { StateGraph, Annotation, MemorySaver, END, START } = require('@langchain/langgraph');
const { PostgresSaver }                                    = require('@langchain/langgraph-checkpoint-postgres');

const traceBus       = require('../traceBus');
const pipelineLog    = require('../../execution/pipeline_logging');
const { makeStepNode } = require('./daily_cycle_node');
const { postAbortAlert } = require('./daily_cycle_helpers');

// `activation` (2026-08-22) runs FIRST: it re-derives strategy_regime_params
// .eligible + strategy_weights_by_regime from the dashboard Strategy
// Activation sliders when a slider moved since the last apply, so a slider
// change takes effect at the NEXT DAILY CYCLE (not the Mon 00:00 ET weekly
// refresh). No-op when nothing changed. Never aborts the chain (see
// daily_cycle_node.js non-abort set).
const STEPS_IN_ORDER = [
  'activation',
  'collect', 'sentiment', 'signals', 'option_hedge', 'ic_gate', 'handoff',
  'trade', 'alpaca', 'reconcile', 'stop_reattach', 'report',
  'pyportfolioopt_shadow', 'health',
];

// Step name → script base name (matches pipeline_orchestrator.py:51-79 STEPS list).
// Step is the logical name used in logs, Discord, traceBus, and the requestedSteps
// subset filter. Script is the file resolveScript looks for in src/pipeline/ or
// src/execution/. Identity-mapped steps (signals → engine? no — signals → engine,
// see below) are listed explicitly for clarity.
const STEP_SCRIPTS = {
  'activation':             'activation_apply',
  'collect':                'run_collector_once',
  'sentiment':              'run_sentiment_step',
  'signals':                'engine',
  'option_hedge':           'run_option_hedge_targets',
  'ic_gate':                'ic_gate_runner',
  'handoff':                'trade_handoff_builder',
  'trade':                  'regime_blended_sizer_live',
  'alpaca':                 'alpaca_executor',
  'reconcile':              'alpaca_reconcile',
  'stop_reattach':          'stop_reattach',
  'report':                 'send_report',
  'pyportfolioopt_shadow':  'pyportfolioopt_shadow',
  'health':                 'daily_health_digest',
};

const DailyCycleState = Annotation.Root({
  runDate:        Annotation(),
  runId:          Annotation(),
  reason:         Annotation(),
  requestedSteps: Annotation(),
  completedSteps: Annotation({
    reducer: (existing, update) => {
      if (update === undefined || update === null) return existing || [];
      // If the node returns the full new list (e.g. [...state.completedSteps, completion]),
      // honor it as-is. If it returns just an appended record (rare), accept either shape.
      return Array.isArray(update) ? update : [...(existing || []), update];
    },
    default: () => [],
  }),
  abortedAt:      Annotation(),
  lastError:      Annotation(),
  env:            Annotation(),
  perStrategyUniverse: Annotation({
    default: () => ({}),
    // No reducer — last writer wins (single per-cycle population)
  }),
});

// ── SP-2 Phase A: per-strategy universe pre-resolution ────────────────────────
function loadPerStrategyUniverse(today, liveStrategies, opts = {}) {
  const { execSync } = require('child_process');
  const timeout = opts.timeout || 15000;
  const cwd = opts.cwd || '/root/openclaw';
  const result = {};
  for (const sid of liveStrategies) {
    try {
      const out = execSync(
        `python3 -m src.strategies.universe_resolver --as-of ${today} --strategy ${sid}`,
        { encoding: 'utf8', timeout, cwd },
      );
      result[sid] = JSON.parse(out);
    } catch (e) {
      console.warn(`[daily-cycle] loadPerStrategyUniverse failed for ${sid}: ${e.message}`);
      result[sid] = [];
    }
  }
  return result;
}

// ── Checkpointer ─────────────────────────────────────────────────────────────
let _checkpointer = null;
let _checkpointerReady = null;

function getCheckpointer() {
  if (_checkpointer) return { checkpointer: _checkpointer, ready: _checkpointerReady };
  if (process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER === '1') {
    _checkpointer = new MemorySaver();
    _checkpointerReady = Promise.resolve();
    return { checkpointer: _checkpointer, ready: _checkpointerReady };
  }
  const uri = process.env.POSTGRES_URI || 'postgresql://openclaw:password@localhost:5432/openclaw';
  _checkpointer = PostgresSaver.fromConnString(uri, { schema: 'langgraph' });
  _checkpointerReady = _checkpointer.setup()
    .then(() => console.log('[daily-cycle] PostgresSaver schema ready'))
    .catch((e) => console.error('[daily-cycle] PostgresSaver setup failed:', e.message));
  return { checkpointer: _checkpointer, ready: _checkpointerReady };
}

// ── Graph build (lazy + memoized) ────────────────────────────────────────────
let _compiled = null;
function getCompiled() {
  if (_compiled) return _compiled;
  const { checkpointer } = getCheckpointer();
  const g = new StateGraph(DailyCycleState);
  for (const step of STEPS_IN_ORDER) g.addNode(step, makeStepNode(step, STEP_SCRIPTS[step]));
  g.addEdge(START, STEPS_IN_ORDER[0]);
  for (let i = 0; i < STEPS_IN_ORDER.length - 1; i++) {
    g.addEdge(STEPS_IN_ORDER[i], STEPS_IN_ORDER[i + 1]);
  }
  g.addEdge(STEPS_IN_ORDER[STEPS_IN_ORDER.length - 1], END);
  _compiled = g.compile({ checkpointer });
  return _compiled;
}

// ── Redis lock (mirrors pipeline_orchestrator.py:152-158) ────────────────────
async function _acquireRunLock(runDate) {
  const Redis = require('ioredis');
  const r = new Redis(process.env.REDIS_URL || 'redis://localhost:6379');
  const key = `engine:run_lock:${runDate}`;
  const lockId = `daily-cycle:${process.pid}:${Date.now()}`;
  const ok = await r.set(key, lockId, 'NX', 'EX', 7200);
  if (!ok) {
    let owner = '?';
    try {
      owner = await r.get(key);
    } catch (_e) { /* best effort */ }
    await r.quit().catch(() => {});
    const err = new Error(`cycle already in progress for ${runDate} (lock held by ${owner})`);
    err.lockHeld = true;
    throw err;
  }
  return { release: async () => { try { await r.del(key); } finally { await r.quit().catch(() => {}); } } };
}

// ── Public API ───────────────────────────────────────────────────────────────
async function runDailyCycleGraph(input) {
  const { runDate, reason = 'scheduled', requestedSteps = null } = input || {};
  if (!runDate) throw new Error('runDate is required');
  const runId    = `run-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const threadId = `daily-cycle:${runDate}`;

  // Redis lock — fail fast on concurrent same-date invocations.
  // Skipped in resume path (see resumeDailyCycle) since the lock is already
  // held by the crashed process's leaked TTL. Skipped entirely in tests
  // (MemorySaver mode) so no Redis is required.
  let lock = null;
  if (process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER !== '1') {
    try {
      lock = await _acquireRunLock(runDate);
    } catch (err) {
      if (err.lockHeld) {
        await pipelineLog.cycleEnd(runDate, runId, 'aborted', '__lock_held__').catch(() => {});
        return { runId, threadId, status: 'aborted', abortedAt: '__lock_held__',
                 lastError: { message: err.message } };
      }
      throw err;
    }
  }

  const { ready } = getCheckpointer();
  await ready;
  const compiled = getCompiled();

  traceBus.startRun(runId, { cycleDate: runDate, threadId, graph: 'daily-cycle' });
  await pipelineLog.cycleStart(runDate, reason, runId);

  const statePayload = {
    runDate,
    runId,
    reason,
    requestedSteps: requestedSteps ? new Set(requestedSteps) : null,
    completedSteps: [],
    abortedAt:      null,
    lastError:      null,
    env:            {},
  };
  const config = { configurable: { thread_id: threadId } };

  try {
    const out = await compiled.invoke(statePayload, config);
    traceBus.endRun(runId, 'ok');
    await pipelineLog.cycleEnd(runDate, runId, 'ok', null);
    return { ...out, runId, threadId, status: 'ok' };
  } catch (err) {
    const abortedAt = err.step || 'unknown';
    traceBus.endRun(runId, 'error', err.message);
    await pipelineLog.cycleEnd(runDate, runId, 'aborted', abortedAt);
    // Read partial state from the checkpoint to surface completedSteps
    const snap = await compiled.getState(config).catch(() => null);
    const partial = (snap && snap.values) || {};
    const lastError = { step: abortedAt, rc: err.rc || null, message: err.message };
    // Surface the abort to #botjohn-log — the health step never runs after an abort,
    // so without this the failure is silent (see 2026-06-22/23 EOD collect OOM).
    await postAbortAlert({ runDate, runId, abortedAt, lastError, reason }).catch(() => {});
    return {
      ...partial,
      runId, threadId,
      status:     'aborted',
      abortedAt,
      lastError,
    };
  } finally {
    if (lock) await lock.release().catch(() => {});
  }
}

async function resumeDailyCycle(runDate) {
  const { ready } = getCheckpointer();
  await ready;
  const compiled = getCompiled();
  const threadId = `daily-cycle:${runDate}`;
  const config = { configurable: { thread_id: threadId } };
  const snap = await compiled.getState(config);
  if (!snap || !snap.next || snap.next.length === 0) {
    return { status: 'no_resume_needed', threadId };
  }
  const runId = (snap.values && snap.values.runId) || `resume-${Date.now()}`;
  traceBus.startRun(runId, { cycleDate: runDate, threadId, graph: 'daily-cycle', resumed: true });
  await pipelineLog.cycleStart(runDate, 'resumed', runId);
  try {
    const out = await compiled.invoke(null, config);
    traceBus.endRun(runId, 'ok');
    await pipelineLog.cycleEnd(runDate, runId, 'ok', null);
    return { ...out, runId, threadId, status: 'ok', resumed: true };
  } catch (err) {
    const abortedAt = err.step || 'unknown';
    traceBus.endRun(runId, 'error', err.message);
    await pipelineLog.cycleEnd(runDate, runId, 'aborted', abortedAt);
    const lastError = { step: err.step, rc: err.rc, message: err.message };
    await postAbortAlert({ runDate, runId, abortedAt, lastError, reason: 'resumed' }).catch(() => {});
    return {
      runId, threadId,
      status:    'aborted',
      abortedAt,
      lastError,
    };
  }
}

async function listThreadState(runDate) {
  const { ready } = getCheckpointer();
  await ready;
  const compiled = getCompiled();
  const threadId = `daily-cycle:${runDate}`;
  return compiled.getState({ configurable: { thread_id: threadId } });
}

module.exports = {
  runDailyCycleGraph,
  resumeDailyCycle,
  listThreadState,
  STEPS_IN_ORDER,
  STEP_SCRIPTS,
  DailyCycleState,
  getCompiled,
  _acquireRunLock,
  loadPerStrategyUniverse,
};
