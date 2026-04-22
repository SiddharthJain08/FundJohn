/**
 * PaperHunter parallel fan-out graph.
 *
 *   dispatch ─► (Send × N) ─► extract_one ─► reduce ─► END
 *
 * Given a list of candidate papers, spawn one PaperHunter subagent per paper
 * in parallel (bounded by `concurrency`), collect results, and return the
 * aggregate (accepts + rejects with reasons). This replaces the serial
 * `for (cand of candidates) { await _runPaperHunter(cand) }` pattern.
 *
 * Wire-up is minimal: callers pass an `extract` function so this graph stays
 * decoupled from the research-orchestrator class. In production that's
 * `(candidate) => orchestrator._runPaperHunter(candidate)`.
 */
const { StateGraph, Annotation, Send, END, START } = require('@langchain/langgraph');
const traceBus = require('../traceBus');

const HuntState = Annotation.Root({
  candidates: Annotation(),
  concurrency: Annotation(),
  results:    Annotation({
    reducer: (a, b) => [...(a || []), ...(b || [])],
    default: () => [],
  }),
  runId: Annotation(),
  extractRef: Annotation(),  // module-level key — see extractRegistry below
});

// Extract functions are non-serializable (closures over orchestrator
// state), so we keep them in a module-level registry keyed by a token
// stored in graph state. The token survives checkpointing; the closure doesn't.
const extractRegistry = new Map();

async function dispatch(state) {
  // Plain node — side effect only. Fan-out happens in the conditional edge.
  traceBus.push({ runId: state.runId, node: 'paperhunter.dispatch', status: 'ok', ts: Date.now(), count: (state.candidates || []).length });
  return {};
}

function dispatchRouter(state) {
  const cs = state.candidates || [];
  if (!cs.length) return 'reduce';
  return cs.map((c, idx) =>
    new Send('extract_one', { candidate: c, index: idx, runId: state.runId, extractRef: state.extractRef })
  );
}

async function extractOne(payload) {
  const { candidate, index, runId, extractRef } = payload;
  const candId = candidate.candidate_id || candidate.id || `cand-${index}`;
  traceBus.push({ runId, node: 'paperhunter.extract', status: 'start', ts: Date.now(), candId });
  const extract = extractRegistry.get(extractRef);
  if (typeof extract !== 'function') {
    const err = `extract fn not registered for ref=${extractRef}`;
    traceBus.push({ runId, node: 'paperhunter.extract', status: 'error', ts: Date.now(), candId, error: err });
    return { results: [{ candidate_id: candId, rejection_reason_if_any: err }] };
  }
  const t0 = Date.now();
  try {
    const out = await extract(candidate);
    traceBus.push({ runId, node: 'paperhunter.extract', status: 'ok', ts: Date.now(), candId, ms: Date.now() - t0, rejected: !!out.rejection_reason_if_any });
    return { results: [out] };
  } catch (err) {
    traceBus.push({ runId, node: 'paperhunter.extract', status: 'error', ts: Date.now(), candId, error: String(err.message || err) });
    return { results: [{ candidate_id: candId, rejection_reason_if_any: `exception: ${err.message}` }] };
  }
}

async function reduce(state) {
  const n = (state.results || []).length;
  const accepted = state.results.filter(r => !r.rejection_reason_if_any).length;
  traceBus.push({ runId: state.runId, node: 'paperhunter.reduce', status: 'ok', ts: Date.now(), total: n, accepted, rejected: n - accepted });
  return {};
}

function build() {
  const g = new StateGraph(HuntState)
    .addNode('dispatch',    dispatch)
    .addNode('extract_one', extractOne)
    .addNode('reduce',      reduce)
    .addEdge(START, 'dispatch')
    .addConditionalEdges('dispatch', dispatchRouter, ['extract_one', 'reduce'])
    .addEdge('extract_one', 'reduce')
    .addEdge('reduce', END);
  return g.compile();
}

let _compiled = null;
function getCompiled() { return _compiled || (_compiled = build()); }

/**
 * Run the fan-out graph.
 *
 * @param {Object}   opts
 * @param {Array}    opts.candidates  — [{candidate_id, source_url, ...}, ...]
 * @param {Function} opts.extract     — async (candidate) => paperhunter output
 * @param {number}   [opts.concurrency=4] — advisory; LangGraph parallelizes Sends
 * @param {string}   [opts.runId]     — for trace correlation
 */
async function runPaperHunt({ candidates, extract, concurrency = 4, runId }) {
  if (!Array.isArray(candidates)) throw new Error('candidates must be an array');
  if (typeof extract !== 'function') throw new Error('extract must be a function');
  const rid = runId || `papers-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ref = `extract-${rid}`;
  extractRegistry.set(ref, extract);
  traceBus.startRun(rid, { type: 'paperhunter', n: candidates.length });
  try {
    const out = await getCompiled().invoke(
      { candidates, concurrency, runId: rid, extractRef: ref },
      { recursionLimit: Math.max(25, candidates.length * 2 + 10) }
    );
    traceBus.endRun(rid, 'ok');
    return { runId: rid, results: out.results || [] };
  } catch (err) {
    traceBus.endRun(rid, 'error', String(err.message || err));
    throw err;
  } finally {
    extractRegistry.delete(ref);
  }
}

module.exports = { runPaperHunt, getCompiled };
