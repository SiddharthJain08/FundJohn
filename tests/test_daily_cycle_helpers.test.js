'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const helpers = require(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));

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
