'use strict';

/**
 * tests/test_daily_cycle_abort_alert.test.js
 *
 * The 2026-06-22/23 EOD compute OOM-aborted at the `collect` step (rc=137) two
 * trading days running and NOTHING alerted anyone — the `health` step (which posts
 * the daily digest) never runs after an abort, so the failure was silent for 2 days.
 * These tests pin the abort-alert helper that closes that gap.
 *
 * Run: node --test tests/test_daily_cycle_abort_alert.test.js
 */

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const helpers = require(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));

test('formatAbortAlert names the step, run, and flags rc=137 as OOM', () => {
  const msg = helpers.formatAbortAlert({
    runDate: '2026-06-23', runId: 'run-abc', abortedAt: 'collect',
    lastError: { step: 'collect', rc: 137, message: 'parquet op write_options exited 137' },
    reason: 'eod-signal-register',
  });
  assert.match(msg, /ABORT/i);
  assert.match(msg, /2026-06-23/);
  assert.match(msg, /collect/);
  assert.match(msg, /run-abc/);
  assert.match(msg, /SIGKILL|OOM/i);    // rc=137 must be flagged as the OOM/SIGKILL class
  assert.match(msg, /137/);
});

test('formatAbortAlert omits the rc hint line when rc is null', () => {
  const msg = helpers.formatAbortAlert({
    runDate: '2026-06-23', runId: 'run-xyz', abortedAt: 'signals',
    lastError: { step: 'signals', rc: null, message: 'engine raised ValueError' },
  });
  assert.match(msg, /signals/);
  // No rc-derived hint line should appear (SIGKILL/OOM/timeout/exit-code).
  assert.doesNotMatch(msg, /SIGKILL|exit code|timed out/i);
});

test('postAbortAlert posts the formatted content when a webhook exists', async () => {
  const calls = [];
  const res = await helpers.postAbortAlert(
    { runDate: '2026-06-23', runId: 'r1', abortedAt: 'collect', lastError: { rc: 137, message: 'x' } },
    {
      getWebhook: async () => 'https://discord/webhook/abc',
      postWebhook: async (url, content) => { calls.push({ url, content }); return { status: 204 }; },
    },
  );
  assert.equal(res.sent, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://discord/webhook/abc');
  assert.match(calls[0].content, /ABORT/i);
});

test('postAbortAlert is a no-op (no post) when no webhook is configured', async () => {
  let posted = false;
  const res = await helpers.postAbortAlert(
    { runDate: '2026-06-23', runId: 'r2', abortedAt: 'collect', lastError: { rc: 137 } },
    { getWebhook: async () => null, postWebhook: async () => { posted = true; return { status: 204 }; } },
  );
  assert.equal(res.sent, false);
  assert.equal(posted, false);
});

test('postAbortAlert is fail-soft — never throws if the post fails', async () => {
  const res = await helpers.postAbortAlert(
    { runDate: '2026-06-23', runId: 'r3', abortedAt: 'collect', lastError: { rc: 137 } },
    {
      getWebhook: async () => 'https://discord/webhook/abc',
      postWebhook: async () => { throw new Error('network down'); },
    },
  );
  assert.equal(res.sent, false);   // resolved, did not throw
});

test('postAbortAlert never blocks the abort path — resolves on overall timeout if the post hangs', async () => {
  // rc=137 means the box is memory-thrashing; a hung pg connect / https POST must
  // not wedge the abort return. The overall timeout backstops it.
  const started = Date.now();
  const res = await helpers.postAbortAlert(
    { runDate: '2026-06-23', runId: 'r4', abortedAt: 'collect', lastError: { rc: 137 } },
    {
      getWebhook: async () => 'https://discord/webhook/abc',
      // slow post (1s) — exceeds the 200ms overall timeout, but still settles
      // (mirrors prod, where per-op timeouts guarantee work settles).
      postWebhook: () => new Promise((resolve) => setTimeout(() => resolve({ status: 204 }), 1000)),
      timeoutMs: 200,
    },
  );
  assert.equal(res.sent, false);
  assert.equal(res.reason, 'timeout');
  assert.ok(Date.now() - started < 900, 'should resolve via the 200ms timeout, not wait for the 1s post');
});
