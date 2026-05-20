'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const { spawnSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');

test('run_mastermind.js --mode critique is dispatched (not unknown)', () => {
  // Gate OFF so the dispatcher logs the skip JSON and exits 0 quickly.
  const result = spawnSync('node', [
    path.join(ROOT, 'src/agent/curators/run_mastermind.js'),
    '--mode', 'critique',
  ], {
    env: { ...process.env, OPENCLAW_MEMO_CRITIQUE: '0' },
    timeout: 15_000,
  });
  const stdout = result.stdout?.toString() || '';
  const stderr = result.stderr?.toString() || '';
  // Case-insensitive "unknown" check to guard against future error-string drift.
  assert.ok(
    !(stdout + stderr).toLowerCase().includes('unknown'),
    `dispatcher should recognize 'critique'. stdout=${stdout.slice(0, 400)} stderr=${stderr.slice(0, 400)}`
  );
  // Positive assertion: dispatcher actually entered runCritique() and logged the gate-off JSON.
  assert.ok(
    stdout.includes('"mode":"critique"') && stdout.includes('"skipped":true'),
    `expected gate-off skip JSON. stdout=${stdout.slice(0, 400)}`
  );
  assert.equal(result.status, 0, `should exit 0 with gate off. status=${result.status}`);
});
