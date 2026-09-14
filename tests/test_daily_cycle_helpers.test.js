'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const helpers = require(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));
const runLock = require(path.join(ROOT, 'src/lib/run_lock.js'));

// QD E2 fix round 1 (2026-09-14): this file's runSubprocess calls never pass
// memoryMax, so on a uid-0 box with a working systemd-run they would
// otherwise wrap every one in a real transient scope. '0' short-circuits in
// wrapCapped BEFORE the availability probe runs, so there's no probe call
// and no stderr warning either. node --test runs this file in its own
// process, so this can't leak into any other test file.
process.env.OPENCLAW_STEP_MEMORY_MAX = '0';

test('skipForSubset honors requestedSteps when present', () => {
  assert.equal(helpers.skipForSubset('collect', { requestedSteps: null }),                          false);
  assert.equal(helpers.skipForSubset('collect', { requestedSteps: undefined }),                     false);
  assert.equal(helpers.skipForSubset('collect', { requestedSteps: new Set(['signals']) }),          true);
  assert.equal(helpers.skipForSubset('collect', { requestedSteps: new Set(['collect','signals']) }), false);
});

test('strictMode reads OPENCLAW_STRICT_EXIT_CODES from env', () => {
  assert.equal(helpers.strictMode({ OPENCLAW_STRICT_EXIT_CODES: '1' }), true);
  assert.equal(helpers.strictMode({ OPENCLAW_STRICT_EXIT_CODES: '0' }), false);
  assert.equal(helpers.strictMode({}),                                  false);
});

test('runSubprocess returns rc=0 + stdout + stderr for successful command', async () => {
  // Use /bin/echo (always present on Linux)
  const out = await helpers.runSubprocess(['echo', 'hello'], { timeoutSec: 5, env: process.env });
  assert.equal(out.rc, 0);
  assert.match(out.stdout || '', /hello/);
  assert.ok(typeof out.durationMs === 'number' && out.durationMs >= 0);
});

test('runSubprocess returns rc=1 for failed command and captures stderr tail', async () => {
  // /bin/false exits 1 with no output; use 'sh -c' to print to stderr
  const out = await helpers.runSubprocess(['sh', '-c', 'echo nope >&2; exit 1'], { timeoutSec: 5, env: process.env });
  assert.equal(out.rc, 1);
  assert.match(out.stderrTail || '', /nope/);
});

test('runSubprocess respects timeout and returns rc=124-equivalent', async () => {
  // sleep 10 with timeoutSec=1 → should kill the proc and return non-zero
  const out = await helpers.runSubprocess(['sleep', '10'], { timeoutSec: 1, env: process.env });
  assert.notEqual(out.rc, 0);
  assert.ok(out.timedOut === true);
});

// ── run_lock gating (QD E1 controller ruling, 2026-09-14): this graph IS
// production whenever OPENCLAW_LANGGRAPH_ORCHESTRATOR=1, so runSubprocess
// must renew the shared run lock before spawning and refuse to spawn if
// that renew reports the lock lost. ────────────────────────────────────────

test('runSubprocess does not spawn when the current run lock renew fails', async () => {
  const fs     = require('node:fs');
  const os     = require('node:os');
  const marker = path.join(os.tmpdir(), `run_lock_test_marker_${process.pid}_${Date.now()}`);
  const fakeRedis = { async get() { return 'otherhost:1:T9'; } }; // never matches our value
  runLock.setCurrent({ r: fakeRedis, runDate: '2026-09-14', value: 'vps1:2:T0' });
  try {
    const out = await helpers.runSubprocess(
      ['node', '-e', `require('fs').writeFileSync(${JSON.stringify(marker)}, 'ran')`],
      { timeoutSec: 5, env: process.env, step: 'trade' },
    );
    assert.equal(out.rc, 75);
    assert.equal(out.lockLost, true);
    assert.match(out.stderrTail, /\[lock\] lost before trade/);
    assert.equal(out.durationMs, 0);
    assert.equal(fs.existsSync(marker), false); // the subprocess never ran
  } finally {
    runLock.clearCurrent();
    try { fs.unlinkSync(marker); } catch (_) { /* never created — fine */ }
  }
});

test('runSubprocess spawns normally when the current run lock renew succeeds', async () => {
  const key = runLock.lockKey('2026-09-14');
  const fakeRedis = {
    store: { [key]: 'vps1:2:T0' },
    async get(k) { return this.store[k]; },
    async expire() { return 1; },
  };
  runLock.setCurrent({ r: fakeRedis, runDate: '2026-09-14', value: 'vps1:2:T0' });
  try {
    const out = await helpers.runSubprocess(['echo', 'hello'], { timeoutSec: 5, env: process.env, step: 'collect' });
    assert.equal(out.rc, 0);
    assert.match(out.stdout || '', /hello/);
    assert.notEqual(out.lockLost, true);
  } finally {
    runLock.clearCurrent();
  }
});

test('runSubprocess behaves exactly as before when no lock is currently held', async () => {
  runLock.clearCurrent(); // tests / one-off runs never call setCurrent()
  const out = await helpers.runSubprocess(['echo', 'hello'], { timeoutSec: 5, env: process.env });
  assert.equal(out.rc, 0);
  assert.match(out.stdout || '', /hello/);
});
