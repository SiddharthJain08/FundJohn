'use strict';
// Tests for src/lib/spawn_timeout.js — the guard that prevents a hung child
// process (e.g. a wedged paperhunter claude-bin, or a stuck arxiv ingest) from
// blocking the weekend research pipeline until the 6h systemd ceiling kills it.
// Root cause of the 2026-06-06/07 saturday-brain failures (stalled at phase=hunt).
//
// Run: node --test tests/test_spawn_timeout.js

const test = require('node:test');
const assert = require('node:assert');
const { spawnWithTimeout } = require('../src/lib/spawn_timeout');

test('kills a hanging child after timeoutMs and reports timedOut', async () => {
  const start = Date.now();
  // `sleep 30` would block for 30s; the 300ms timeout must kill it first.
  const res = await spawnWithTimeout('sleep', ['30'], {}, { timeoutMs: 300 });
  const elapsed = Date.now() - start;
  assert.strictEqual(res.timedOut, true, 'should report timedOut=true');
  assert.ok(elapsed < 5000, `should settle promptly after SIGKILL, took ${elapsed}ms`);
});

test('returns code 0 + stdout for a fast child within the timeout', async () => {
  const res = await spawnWithTimeout(
    'node', ['-e', 'process.stdout.write("hi")'], {}, { timeoutMs: 5000 });
  assert.strictEqual(res.timedOut, false, 'fast child should not time out');
  assert.strictEqual(res.code, 0);
  assert.strictEqual(res.stdout, 'hi');
});

test('captures non-zero exit code without timing out', async () => {
  const res = await spawnWithTimeout(
    'node', ['-e', 'process.stderr.write("boom"); process.exit(3)'], {}, { timeoutMs: 5000 });
  assert.strictEqual(res.timedOut, false);
  assert.strictEqual(res.code, 3);
  assert.strictEqual(res.stderr, 'boom');
});

test('invokes onTimeout callback when the child is killed', async () => {
  let called = false;
  const res = await spawnWithTimeout(
    'sleep', ['30'], {}, { timeoutMs: 200, onTimeout: () => { called = true; } });
  assert.strictEqual(res.timedOut, true);
  assert.strictEqual(called, true, 'onTimeout should fire on kill');
});
