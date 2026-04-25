/**
 * FundJohn pipeline as a LangGraph StateGraph.
 *
 *   datajohn ─► researchjohn ─► tradejohn ─► (interrupt: HITL) ─► botjohn ─► END
 *                                       │
 *                                       └─► (no signals) ─► END
 *
 * - PostgresSaver:  durable checkpoints in the openclaw DB so a run can resume
 *                   across johnbot restarts (table: langgraph_checkpoints).
 * - interruptBefore: ['botjohn'] — pause for human approval; dashboard or
 *                    Discord resumes with approve/veto via /api/runs/:id/resume.
 * - Conditional edge: tradejohn → botjohn iff tradejohn produced any signals,
 *                    otherwise short-circuit to END.
 * - Non-serializable values (notify fn, live strategy list) travel via
 *                    config.configurable and are NOT written to checkpoint state.
 */
const { StateGraph, Annotation, END, START } = require('@langchain/langgraph');
const { PostgresSaver } = require('@langchain/langgraph-checkpoint-postgres');
const path = require('path');
const traceBus = require('./traceBus');
const cycleCache = require('./services/cycle-cache');

// LangSmith auto-tracing: if the user sets LANGSMITH_API_KEY in .env, flip
// LANGCHAIN_TRACING_V2=true so every graph invocation lands in their dashboard.
// No-op when the key is absent, so this costs nothing by default.
if (process.env.LANGSMITH_API_KEY && !process.env.LANGCHAIN_TRACING_V2) {
  process.env.LANGCHAIN_TRACING_V2 = 'true';
  process.env.LANGCHAIN_PROJECT = process.env.LANGCHAIN_PROJECT || 'fundjohn';
  console.log('[graph] LangSmith tracing enabled (project=' + process.env.LANGCHAIN_PROJECT + ')');
}

const CycleState = Annotation.Root({
  cycleDate:       Annotation(),
  portfolioState:  Annotation(),
  memoDir:         Annotation(),
  reportPath:      Annotation(),
  threadId:        Annotation(),
  workspace:       Annotation(),
  dataResult:      Annotation(),
  researchResult:  Annotation(),
  tradeResult:     Annotation(),
  botResult:       Annotation(),
  signalCount:     Annotation(),
  runId:           Annotation(),
  approval:        Annotation(), // 'approved' | 'vetoed' | undefined
});

function emit(runId, node, status, extra = {}) {
  traceBus.push({ runId, node, status, ts: Date.now(), ...extra });
}

function notifierFor(config) {
  return (config?.configurable?.notify) || (() => {});
}

// ── Nodes ────────────────────────────────────────────────────────────────────
async function datajohnNode(state, config) {
  const notify = notifierFor(config);
  emit(state.runId, 'datajohn', 'start', { cycleDate: state.cycleDate });
  notify(`📈 datajohn: running hardcoded pipeline for ${state.cycleDate}`);
  const runner = require('../execution/runner');
  const dataResult = await runner.runDailyClose('default', state.memoDir);
  emit(state.runId, 'datajohn', 'ok', { summary: summarize(dataResult) });
  return { dataResult };
}

async function researchjohnNode(state, config) {
  const notify = notifierFor(config);
  emit(state.runId, 'researchjohn', 'start');
  const { init } = require('./subagents/swarm');
  const researchResult = await init({
    type:      'researchjohn',
    ticker:    state.cycleDate,
    workspace: state.workspace,
    threadId:  state.threadId,
    notify,
    mode:      'PM_TASK',
    prompt:    `CYCLE_DATE=${state.cycleDate}\nMEMO_DIR=${state.memoDir}`,
  });
  emit(state.runId, 'researchjohn', 'ok', {
    subagentId: researchResult.subagentId,
    duration: researchResult.duration,
  });
  return { researchResult };
}

async function tradejohnNode(state, config) {
  const notify = notifierFor(config);
  emit(state.runId, 'tradejohn', 'start');
  const { init } = require('./subagents/swarm');
  const tradeResult = await init({
    type:      'tradejohn',
    ticker:    state.cycleDate,
    workspace: state.workspace,
    threadId:  state.threadId,
    notify,
    mode:      'PM_TASK',
    prompt:    `CYCLE_DATE=${state.cycleDate}\nREPORT_PATH=${state.reportPath}\nPORTFOLIO_STATE=${JSON.stringify(state.portfolioState || {})}`,
  });
  const signalCount = countSignals(tradeResult);
  emit(state.runId, 'tradejohn', 'ok', {
    subagentId: tradeResult.subagentId,
    duration: tradeResult.duration,
    signalCount,
  });
  return { tradeResult, signalCount };
}

async function botjohnNode(state, config) {
  const notify = notifierFor(config);
  emit(state.runId, 'botjohn', 'start', { approval: state.approval });
  // If operator vetoed during HITL pause, short-circuit with a recorded result.
  if (state.approval === 'vetoed') {
    const botResult = { vetoed: true, by: 'operator', at: Date.now() };
    emit(state.runId, 'botjohn', 'ok', { vetoed: true });
    notify('🛑 botjohn: cycle vetoed by operator; no trade approvals.');
    return { botResult };
  }
  const { init } = require('./subagents/swarm');
  const signalsPath = path.join(state.workspace, 'output', 'signals', `${state.cycleDate}_signals.md`);
  const botResult = await init({
    type:      'botjohn',
    ticker:    state.cycleDate,
    workspace: state.workspace,
    threadId:  state.threadId,
    notify,
    mode:      'PM_TASK',
    prompt:    `Cycle review for ${state.cycleDate}. Read AGENTS.md standing orders. Review trade signals at ${signalsPath}. Approve signals with EV > 0 within 3% NAV limit per SO-4. Auto-veto negative EV. Escalate any strategy with max_drawdown > 20% per SO-5. Post cycle digest to #ops.`,
  });
  emit(state.runId, 'botjohn', 'ok', {
    subagentId: botResult.subagentId,
    duration: botResult.duration,
  });
  return { botResult };
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function summarize(obj) {
  if (obj == null) return null;
  try {
    const s = JSON.stringify(obj);
    return s.length > 400 ? s.slice(0, 400) + '…' : s;
  } catch { return String(obj).slice(0, 400); }
}

function countSignals(tradeResult) {
  if (!tradeResult) return 0;
  // Best-effort: tradejohn's output is a string; look for an explicit count
  // hint, or count lines that look like trade entries. Falls back to 1 if
  // the output is non-empty but unparseable (so we don't spuriously skip botjohn).
  const out = tradeResult.output || tradeResult.result || '';
  if (typeof out !== 'string') return 1;
  const explicit = out.match(/signals?_generated\s*[:=]\s*(\d+)/i);
  if (explicit) return parseInt(explicit[1], 10);
  const noneMatch = /\b(no\s+signals|zero\s+signals|0\s+signals)\b/i.test(out);
  if (noneMatch) return 0;
  return out.trim() ? 1 : 0;
}

function afterTradejohn(state) {
  return (state.signalCount && state.signalCount > 0) ? 'botjohn' : END;
}

// ── Checkpointer ─────────────────────────────────────────────────────────────
let _checkpointer = null;
let _checkpointerReady = null;

function getCheckpointer() {
  if (_checkpointer) return { checkpointer: _checkpointer, ready: _checkpointerReady };
  const uri = process.env.POSTGRES_URI || 'postgresql://openclaw:password@localhost:5432/openclaw';
  // Use a dedicated schema so we don't collide with OpenClaw's legacy
  // `checkpoints` table in public.
  _checkpointer = PostgresSaver.fromConnString(uri, { schema: 'langgraph' });
  _checkpointerReady = _checkpointer.setup()
    .then(() => console.log('[graph] PostgresSaver schema ready'))
    .catch((e) => console.error('[graph] PostgresSaver setup failed:', e.message));
  return { checkpointer: _checkpointer, ready: _checkpointerReady };
}

// ── Graph build ──────────────────────────────────────────────────────────────
let _compiled = null;
function getCompiled() {
  if (_compiled) return _compiled;
  const { checkpointer } = getCheckpointer();
  const g = new StateGraph(CycleState)
    .addNode('datajohn',     datajohnNode)
    .addNode('researchjohn', researchjohnNode)
    .addNode('tradejohn',    tradejohnNode)
    .addNode('botjohn',      botjohnNode)
    .addEdge(START, 'datajohn')
    .addEdge('datajohn',     'researchjohn')
    .addEdge('researchjohn', 'tradejohn')
    .addConditionalEdges('tradejohn', afterTradejohn, { botjohn: 'botjohn', [END]: END })
    .addEdge('botjohn', END);

  _compiled = g.compile({
    checkpointer,
    interruptBefore: ['botjohn'],
  });
  return _compiled;
}

async function runCycleGraph(input) {
  const { ready } = getCheckpointer();
  await ready;
  const compiled = getCompiled();
  const runId = `run-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const threadId = input.threadId || runId;
  const workspace = process.env.OPENCLAW_DIR || '/root/openclaw';
  traceBus.startRun(runId, { cycleDate: input.cycleDate, threadId });

  // State-only payload (serializable) — notify stays in configurable.
  const statePayload = {
    cycleDate:      input.cycleDate,
    portfolioState: input.portfolioState,
    memoDir:        input.memoDir,
    reportPath:     input.reportPath,
    threadId,
    workspace,
    runId,
  };
  const config = { configurable: { thread_id: threadId, notify: input.notify } };

  try {
    const out = await compiled.invoke(statePayload, config);
    const snap = await compiled.getState(config);
    if (snap?.next?.length) {
      // Interrupted — waiting for HITL. Don't clear cycle cache yet; resume
      // path may still want the cached data.
      traceBus.endRun(runId, 'awaiting_approval', null);
      emit(runId, 'hitl', 'pending', { next: snap.next });
      return { ...out, runId, threadId, status: 'awaiting_approval', next: snap.next };
    }
    traceBus.endRun(runId, 'ok');
    // Cycle terminated normally (botjohn END or tradejohn-no-signals END);
    // drop cycle keys so the namespace doesn't sit in Redis until 24h TTL.
    await cycleCache.clear(threadId).catch(() => {});
    return { ...out, runId, threadId, status: 'ok' };
  } catch (err) {
    traceBus.endRun(runId, 'error', String(err.message || err));
    // On error, also drop cycle keys — failed cycles should not leak data
    // into a manually-restarted retry.
    await cycleCache.clear(threadId).catch(() => {});
    throw err;
  }
}

async function resumeCycle({ threadId, approval, notify }) {
  if (!threadId) throw new Error('threadId required');
  if (!['approved', 'vetoed'].includes(approval)) {
    throw new Error(`approval must be 'approved' or 'vetoed', got ${approval}`);
  }
  const { ready } = getCheckpointer();
  await ready;
  const compiled = getCompiled();
  const config = { configurable: { thread_id: threadId, notify: notify || (() => {}) } };
  const snap = await compiled.getState(config);
  if (!snap?.next?.length) {
    throw new Error(`thread ${threadId} is not awaiting approval`);
  }
  const runId = snap.values?.runId || threadId;
  emit(runId, 'hitl', approval);
  // Inject the approval decision into state, then continue.
  await compiled.updateState(config, { approval });
  const out = await compiled.invoke(null, config);
  traceBus.endRun(runId, approval === 'vetoed' ? 'vetoed' : 'ok');
  // Resume path also terminates the cycle; drop cycle keys.
  await cycleCache.clear(threadId).catch(() => {});
  return { ...out, runId, threadId, status: approval === 'vetoed' ? 'vetoed' : 'ok' };
}

async function listThreadState(threadId) {
  const { ready } = getCheckpointer();
  await ready;
  const compiled = getCompiled();
  const snap = await compiled.getState({ configurable: { thread_id: threadId } });
  return snap;
}

module.exports = { runCycleGraph, resumeCycle, listThreadState, getCompiled, CycleState };
