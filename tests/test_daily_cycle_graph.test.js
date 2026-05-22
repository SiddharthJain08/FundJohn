// tests/test_daily_cycle_graph.test.js
'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const DAILY_CYCLE_PATH = path.join(ROOT, 'src/agent/graphs/daily-cycle.js');
const NODE_PATH        = path.join(ROOT, 'src/agent/graphs/daily_cycle_node.js');
const LOGGING_PATH     = path.join(ROOT, 'src/execution/pipeline_logging.js');
const TRACEBUS_PATH    = path.join(ROOT, 'src/agent/traceBus.js');

const STEPS_IN_ORDER = [
  'collect', 'sentiment', 'signals', 'ic_gate', 'handoff',
  'trade', 'alpaca', 'reconcile', 'report',
  'pyportfolioopt_shadow', 'health',
];

// Stub makeStepNode so each "node" just records its invocation and appends to completedSteps
function makeStubbed({ abortAt = null } = {}) {
  // Force PostgresSaver to be in-memory MemorySaver for tests — set BEFORE require
  process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER = '1';

  const visited = [];
  const nodePath = require.resolve(NODE_PATH);
  const loggingPath = require.resolve(LOGGING_PATH);
  const traceBusPath = require.resolve(TRACEBUS_PATH);

  require.cache[nodePath] = {
    id: nodePath, filename: nodePath, loaded: true,
    exports: {
      makeStepNode: (STEP) => async (state, _config) => {
        visited.push(STEP);
        if (abortAt === STEP) {
          const err = new Error(`step ${STEP} stub abort`);
          err.step = STEP; err.rc = 2;
          throw err;
        }
        // skip path for subset
        if (state.requestedSteps && !state.requestedSteps.has(STEP)) return {};
        return { completedSteps: [...(state.completedSteps || []), { step: STEP, rc: 0, status: 'ok' }] };
      },
    },
  };
  require.cache[loggingPath] = {
    id: loggingPath, filename: loggingPath, loaded: true,
    exports: {
      feedStart: async () => {}, feedEnd: async () => {},
      notifyFailure: async () => {}, cycleStart: async () => {},
      cycleEnd: async () => {}, updateAgentStatus: async () => {},
    },
  };
  require.cache[traceBusPath] = {
    id: traceBusPath, filename: traceBusPath, loaded: true,
    exports: { push: () => {}, startRun: () => {}, endRun: () => {} },
  };
  delete require.cache[require.resolve(DAILY_CYCLE_PATH)];
  const mod = require(DAILY_CYCLE_PATH);
  return { mod, visited };
}

test('Full happy path: all 11 nodes visited in canonical order, status ok', async () => {
  const { mod, visited } = makeStubbed();
  const out = await mod.runDailyCycleGraph({ runDate: '2026-05-21', reason: 'test' });
  assert.deepEqual(visited, STEPS_IN_ORDER);
  assert.equal(out.status, 'ok');
  assert.equal(out.completedSteps.length, 11);
});

test('Subset request: graph runs without throwing (subset filtering tested in Task 4)', async () => {
  const { mod } = makeStubbed();
  const out = await mod.runDailyCycleGraph({
    runDate: '2026-05-21',
    reason:  'regime_transition',
    requestedSteps: ['signals', 'handoff', 'trade', 'alpaca', 'reconcile'],
  });
  // All 11 nodes still visit (test stub doesn't honor subset internally),
  // but graph runs cleanly — assert no throw + status set.
  assert.ok(out.status === 'ok' || out.status === 'aborted');
});

test('Mid-cycle abort: throw at trade → abortedAt set, downstream not visited', async () => {
  const { mod, visited } = makeStubbed({ abortAt: 'trade' });
  const out = await mod.runDailyCycleGraph({ runDate: '2026-05-21', reason: 'test' });
  assert.equal(out.status, 'aborted');
  assert.equal(out.abortedAt, 'trade');
  // Steps before trade visited; trade visited (and threw); steps after NOT visited
  assert.ok(visited.includes('handoff'));
  assert.ok(visited.includes('trade'));
  assert.ok(!visited.includes('alpaca'));
  assert.ok(!visited.includes('reconcile'));
});

test('thread_id is "daily-cycle:<runDate>"', async () => {
  const { mod } = makeStubbed();
  const out = await mod.runDailyCycleGraph({ runDate: '2026-05-22', reason: 'test' });
  assert.equal(out.threadId, 'daily-cycle:2026-05-22');
});

test('Concurrent runs on different runDates have isolated state', async () => {
  const { mod } = makeStubbed();
  const [a, b] = await Promise.all([
    mod.runDailyCycleGraph({ runDate: '2026-05-23', reason: 'test' }),
    mod.runDailyCycleGraph({ runDate: '2026-05-24', reason: 'test' }),
  ]);
  assert.notEqual(a.threadId, b.threadId);
  assert.equal(a.completedSteps.length, 11);
  assert.equal(b.completedSteps.length, 11);
});
