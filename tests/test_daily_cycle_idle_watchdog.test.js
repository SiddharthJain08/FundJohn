'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT    = path.resolve(__dirname, '..');
const helpers = require(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));

// QD E2 fix round 1 (2026-09-14) convention, mirrored here (task-8 brief's
// draft omitted it): pin OPENCLAW_STEP_MEMORY_MAX='0' so runSubprocess's
// wrapCapped short-circuits before the systemd-run availability probe on
// this uid-0 box — no real transient scope, no probe call, no stderr warn.
// node --test runs this file in its own process, so this can't leak into
// any other test file.
process.env.OPENCLAW_STEP_MEMORY_MAX = '0';

test('a silent child is killed by the stdout-idle watchdog, not the wall clock', async () => {
  const t0 = Date.now();
  // Emits once, then goes quiet for far longer than the idle budget.
  // Deviation from the brief's literal 'echo alive; sleep 30' (2026-09-22):
  // on this box /bin/sh forks a child for the trailing `sleep` instead of
  // exec-replacing itself when it follows a builtin (`echo`) in a ';'-joined
  // script. SIGTERM to the shell's own pid then kills the shell but leaves
  // `sleep 30` orphaned, still holding the stdout pipe open — Node's
  // 'close' event (which waits for stdio EOF, not just process exit) then
  // doesn't fire until that orphan exits 30s later, and the test's
  // `elapsed < 20` assertion (correctly) fails. `exec sleep 30` makes the
  // shell replace its own image with `sleep`, so SIGTERM lands on the real
  // sleeping process directly — no orphan, no held-open pipe. This is a
  // test-construction fix only; runSubprocess's wedge detection itself
  // already fired correctly at ~5s in both cases (verified manually).
  const out = await helpers.runSubprocess(
    ['sh', '-c', 'echo alive; exec sleep 30'],
    { timeoutSec: 25, stdoutIdleMaxSec: 2, env: process.env });
  const elapsed = (Date.now() - t0) / 1000;
  assert.equal(out.wedged, true);
  assert.equal(out.rc, 125);
  assert.equal(out.timedOut, false);
  assert.ok(elapsed < 20, `killed at ${elapsed}s — should be ~idle budget, not the wall clock`);
  assert.match(out.stderrTail || '', /\[wedge\]/);
});

test('a chatty child is never wedged even past the idle budget', async () => {
  const out = await helpers.runSubprocess(
    ['sh', '-c', 'for i in 1 2 3 4 5 6; do echo tick; sleep 0.5; done'],
    { timeoutSec: 20, stdoutIdleMaxSec: 2, env: process.env });
  assert.equal(out.rc, 0);
  assert.equal(out.wedged, false);
  assert.match(out.stdout || '', /tick/);
});

test('stderr output also counts as liveness', async () => {
  const out = await helpers.runSubprocess(
    ['sh', '-c', 'for i in 1 2 3 4 5 6; do echo tick >&2; sleep 0.5; done'],
    { timeoutSec: 20, stdoutIdleMaxSec: 2, env: process.env });
  assert.equal(out.rc, 0);
  assert.equal(out.wedged, false);
});

test('the wall-clock timeout still wins when it fires first', async () => {
  const out = await helpers.runSubprocess(
    ['sh', '-c', 'sleep 10'],
    { timeoutSec: 1, stdoutIdleMaxSec: 30, env: process.env });
  assert.equal(out.timedOut, true);
  assert.equal(out.rc, 124);
  assert.equal(out.wedged, false);
});

test('the idle budget defaults to STEP_STDOUT_IDLE_MAX_S, matching the Python twin', () => {
  const old = process.env.STEP_STDOUT_IDLE_MAX_S;
  delete process.env.STEP_STDOUT_IDLE_MAX_S;
  try {
    assert.equal(helpers.stdoutIdleMaxSec(), 600);
    process.env.STEP_STDOUT_IDLE_MAX_S = '90';
    assert.equal(helpers.stdoutIdleMaxSec(), 90);
  } finally {
    if (old === undefined) delete process.env.STEP_STDOUT_IDLE_MAX_S;
    else process.env.STEP_STDOUT_IDLE_MAX_S = old;
  }
});

test('formatAbortAlert names the wedge rc', () => {
  const msg = helpers.formatAbortAlert({
    runDate: '2026-09-14', runId: 'r1', abortedAt: 'collect',
    lastError: { rc: 125, message: 'wedged' }, reason: 'scheduled',
  });
  assert.match(msg, /wedge/i);
});
