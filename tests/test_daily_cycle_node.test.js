// tests/test_daily_cycle_node.test.js
'use strict';

const { test, beforeEach } = require('node:test');
const assert               = require('node:assert/strict');
const path                 = require('node:path');

const ROOT = path.resolve(__dirname, '..');

// IMPORTANT: use require.resolve so the mocked path matches whatever Node's
// internal resolution produces for the module's `require()` calls. Plain
// path.join can disagree (symlinks, drive letters on Windows, etc.).
const NODE_PATH     = require.resolve(path.join(ROOT, 'src/agent/graphs/daily_cycle_node.js'));
const HELPERS_PATH  = require.resolve(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));
const RESOLVE_PATH  = require.resolve(path.join(ROOT, 'src/execution/resolve_script.js'));
const LOGGING_PATH  = require.resolve(path.join(ROOT, 'src/execution/pipeline_logging.js'));
const TRACEBUS_PATH = require.resolve(path.join(ROOT, 'src/agent/traceBus.js'));

// Build a stubbed makeNode by injecting fake helpers + logger + traceBus
function makeStubbedFactory({ rc, stderrTail = '', durationMs = 100, throwSpawn = false, timedOut = false } = {}) {
  const traceEvents = [];
  const logCalls    = [];

  require.cache[HELPERS_PATH] = {
    id: HELPERS_PATH, filename: HELPERS_PATH, loaded: true,
    exports: {
      skipForSubset: (step, state) => {
        if (!state || !state.requestedSteps) return false;
        const req = state.requestedSteps;
        if (req instanceof Set) return !req.has(step);
        if (Array.isArray(req)) return !req.includes(step);
        return false;
      },
      strictMode: (env) => env.OPENCLAW_STRICT_EXIT_CODES === '1',
      runSubprocess: async () => {
        if (throwSpawn) throw new Error('spawn explode');
        return { rc, stderrTail, durationMs, stdout: '', timedOut };
      },
    },
  };
  require.cache[RESOLVE_PATH] = {
    id: RESOLVE_PATH, filename: RESOLVE_PATH, loaded: true,
    exports: { resolveScript: () => ({ argv: ['echo', 'fake'], timeoutSec: 60 }) },
  };
  require.cache[LOGGING_PATH] = {
    id: LOGGING_PATH, filename: LOGGING_PATH, loaded: true,
    exports: {
      feedStart:      async (...args) => logCalls.push(['feedStart',     args]),
      feedEnd:        async (...args) => logCalls.push(['feedEnd',       args]),
      notifyFailure:  async (...args) => logCalls.push(['notifyFailure', args]),
    },
  };
  require.cache[TRACEBUS_PATH] = {
    id: TRACEBUS_PATH, filename: TRACEBUS_PATH, loaded: true,
    exports: {
      push: (ev) => traceEvents.push(ev),
      startRun: () => {},
      endRun:   () => {},
    },
  };
  delete require.cache[require.resolve(NODE_PATH)];
  const { makeStepNode } = require(NODE_PATH);
  return { makeStepNode, traceEvents, logCalls };
}

const BASE_STATE = {
  runDate:        '2026-05-21',
  runId:          'run-test-1',
  reason:         'scheduled',
  requestedSteps: null,
  completedSteps: [],
  env:            {},
};

test('subset-skip path → no subprocess, traceBus.push("skipped"), no Discord posts', async () => {
  const { makeStepNode, traceEvents, logCalls } = makeStubbedFactory({ rc: 0 });
  const node = makeStepNode('collect');
  const out = await node({ ...BASE_STATE, requestedSteps: new Set(['signals']) });
  assert.deepEqual(out, {});
  assert.equal(logCalls.length, 0);
  assert.equal(traceEvents.length, 1);
  assert.equal(traceEvents[0].status, 'skipped');
  assert.equal(traceEvents[0].node,   'collect');
});

test('success (rc=0) → feedStart + feedEnd("ok"), completedSteps appended, traceBus ok event', async () => {
  const { makeStepNode, traceEvents, logCalls } = makeStubbedFactory({ rc: 0, durationMs: 250 });
  const node = makeStepNode('collect');
  const out = await node(BASE_STATE);
  assert.equal(out.completedSteps.length, 1);
  assert.equal(out.completedSteps[0].step,   'collect');
  assert.equal(out.completedSteps[0].rc,     0);
  assert.equal(out.completedSteps[0].status, 'ok');
  assert.equal(logCalls[0][0], 'feedStart');
  assert.equal(logCalls[1][0], 'feedEnd');
  assert.equal(logCalls[1][1][1], 'ok');
  assert.ok(traceEvents.some(e => e.status === 'start'));
  assert.ok(traceEvents.some(e => e.status === 'ok'));
});

test('warn-and-continue (rc=1, OPENCLAW_STRICT_EXIT_CODES unset) → no throw, status=warn', async () => {
  const { makeStepNode, logCalls } = makeStubbedFactory({ rc: 1, stderrTail: 'a warning' });
  const node = makeStepNode('collect');
  const out = await node({ ...BASE_STATE, env: {} });
  assert.equal(out.completedSteps.length, 1);
  assert.equal(out.completedSteps[0].status, 'warn');
  assert.equal(logCalls[1][0], 'feedEnd');
  assert.equal(logCalls[1][1][1], 'warn');
});

test('strict-mode rc=1 → throw with err.step + err.rc + notifyFailure', async () => {
  const { makeStepNode, logCalls } = makeStubbedFactory({ rc: 1, stderrTail: 'strict-mode fail' });
  const node = makeStepNode('collect');
  await assert.rejects(
    () => node({ ...BASE_STATE, env: { OPENCLAW_STRICT_EXIT_CODES: '1' } }),
    (err) => {
      assert.equal(err.step, 'collect');
      assert.equal(err.rc,   1);
      return true;
    }
  );
  assert.ok(logCalls.some(([fn]) => fn === 'notifyFailure'));
});

test('rc=2 → throw always, notifyFailure called with correct channel via STEP_FAILURE_CHANNEL', async () => {
  const { makeStepNode, logCalls } = makeStubbedFactory({ rc: 2, stderrTail: 'hard fail' });
  const nodeTrade = makeStepNode('trade');  // trade → #trade-reports
  await assert.rejects(
    () => nodeTrade({ ...BASE_STATE, env: {} }),
    (err) => { assert.equal(err.step, 'trade'); assert.equal(err.rc, 2); return true; }
  );
  const failCall = logCalls.find(([fn]) => fn === 'notifyFailure');
  assert.ok(failCall);
  assert.equal(failCall[1][0], 'trade'); // step name passed through
});

test('subprocess timeout → throw with timedOut:true preserved in completion record path', async () => {
  const { makeStepNode } = makeStubbedFactory({ rc: 124, stderrTail: 'timed out', timedOut: true });
  const node = makeStepNode('collect');
  await assert.rejects(
    () => node({ ...BASE_STATE, env: {} }),
    (err) => { assert.equal(err.rc, 124); return true; }
  );
});

// `activation` (2026-08-22): re-applies the dashboard activation sliders ahead
// of `signals`. A failed re-apply must NEVER abort the chain — last week's
// eligibility is still a valid book, an empty COMPUTED set is not. Same
// exemption as `sentiment`; the alert still fires.
test('activation rc=1 in STRICT mode → warn + notifyFailure, NO throw (chain continues)', async () => {
  const { makeStepNode, logCalls } = makeStubbedFactory({ rc: 1, stderrTail: 'assigner failed' });
  const node = makeStepNode('activation', 'activation_apply');
  const out = await node({ ...BASE_STATE, env: { OPENCLAW_STRICT_EXIT_CODES: '1' } });
  assert.equal(out.completedSteps.length, 1);
  assert.equal(out.completedSteps[0].status, 'warn');
  assert.ok(logCalls.some(([fn]) => fn === 'notifyFailure'));
  assert.equal(logCalls.at(-1)[0], 'feedEnd');
  assert.equal(logCalls.at(-1)[1][1], 'warn');
});

test('activation rc=2 (hard fail) → still warn, NO throw', async () => {
  const { makeStepNode, logCalls } = makeStubbedFactory({ rc: 2, stderrTail: 'spawn explode' });
  const node = makeStepNode('activation', 'activation_apply');
  const out = await node({ ...BASE_STATE, env: { OPENCLAW_STRICT_EXIT_CODES: '1' } });
  assert.equal(out.completedSteps[0].status, 'warn');
  assert.ok(logCalls.some(([fn]) => fn === 'notifyFailure'));
});
