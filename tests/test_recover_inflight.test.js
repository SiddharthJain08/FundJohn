'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const RECOVER_PATH      = path.join(ROOT, 'src/agent/graphs/recover_inflight.js');
const DAILY_CYCLE_PATH  = path.join(ROOT, 'src/agent/graphs/daily-cycle.js');
const LOGGING_PATH      = path.join(ROOT, 'src/execution/pipeline_logging.js');

function makeStubbed({ inFlight = false, nextSteps = [] } = {}) {
  const resumeCalls = [];
  const cycleStartCalls = [];
  require.cache[DAILY_CYCLE_PATH] = {
    id: DAILY_CYCLE_PATH, filename: DAILY_CYCLE_PATH, loaded: true,
    exports: {
      listThreadState: async (_runDate) =>
        inFlight ? { next: nextSteps, values: { runId: 'r1', runDate: _runDate } } : null,
      resumeDailyCycle: async (runDate) => {
        resumeCalls.push(runDate);
        return { runDate, status: 'ok', resumed: true };
      },
    },
  };
  require.cache[LOGGING_PATH] = {
    id: LOGGING_PATH, filename: LOGGING_PATH, loaded: true,
    exports: {
      cycleStart: async (...args) => cycleStartCalls.push(args),
      _safePost: async () => {},
    },
  };
  delete require.cache[require.resolve(RECOVER_PATH)];
  const mod = require(RECOVER_PATH);
  return { mod, resumeCalls, cycleStartCalls };
}

test('No in-flight thread → silent exit, no resume calls', async () => {
  const { mod, resumeCalls } = makeStubbed({ inFlight: false });
  process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR = '1';
  const out = await mod.recoverInflight();
  assert.equal(out.recovered, false);
  assert.equal(resumeCalls.length, 0);
});

test('In-flight thread with next step → resume invoked', async () => {
  const { mod, resumeCalls } = makeStubbed({ inFlight: true, nextSteps: ['trade'] });
  process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR = '1';
  const out = await mod.recoverInflight();
  assert.equal(out.recovered, true);
  assert.ok(resumeCalls.length >= 1);
});

test('Flag OFF → skip recovery entirely (legacy orchestrator owns it)', async () => {
  const { mod, resumeCalls } = makeStubbed({ inFlight: true, nextSteps: ['trade'] });
  process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR = '0';
  const out = await mod.recoverInflight();
  assert.equal(out.recovered, false);
  assert.equal(out.skipped, true);
  assert.equal(resumeCalls.length, 0);
});
