/**
 * In-memory ring buffer for LangGraph run traces.
 *
 * Shared between the graph nodes (which push events) and the dashboard
 * (which reads /api/traces and streams /api/stream/traces SSE).
 *
 * Not persisted — crashing the johnbot process loses trace history, but
 * durable per-subagent state lives in Postgres `checkpoints` + Redis
 * `subagent:*`. The bus is for live pipeline visualization only.
 */
const { EventEmitter } = require('events');

const MAX_EVENTS = 2000;
const MAX_RUNS = 100;

const events = [];
const runs = new Map();
const bus = new EventEmitter();
bus.setMaxListeners(50);

function push(event) {
  events.push(event);
  if (events.length > MAX_EVENTS) events.splice(0, events.length - MAX_EVENTS);
  const run = runs.get(event.runId);
  if (run) {
    run.lastNode = event.node;
    run.lastStatus = event.status;
    run.updatedAt = event.ts;
    run.events.push(event);
    if (run.events.length > 200) run.events.splice(0, run.events.length - 200);
  }
  bus.emit('event', event);
}

function startRun(runId, meta = {}) {
  const run = {
    runId,
    startedAt: Date.now(),
    updatedAt: Date.now(),
    status: 'running',
    meta,
    events: [],
  };
  runs.set(runId, run);
  if (runs.size > MAX_RUNS) {
    const oldest = [...runs.keys()][0];
    runs.delete(oldest);
  }
  bus.emit('run', run);
  return run;
}

function endRun(runId, status, error) {
  const run = runs.get(runId);
  if (!run) return;
  run.status = status;
  run.finishedAt = Date.now();
  run.updatedAt = Date.now();
  if (error) run.error = error;
  bus.emit('run', run);
}

function listRuns() {
  return [...runs.values()].sort((a, b) => b.startedAt - a.startedAt);
}

function getRun(runId) {
  return runs.get(runId);
}

function recentEvents(limit = 200) {
  return events.slice(-limit);
}

module.exports = { push, startRun, endRun, listRuns, getRun, recentEvents, bus };
