'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const { spawnSync } = require('node:child_process');

const ROOT = path.resolve(__dirname, '..');

test('run_mastermind.js --mode critique is a recognized mode', () => {
  const result = spawnSync('node', [
    path.join(ROOT, 'src/agent/curators/run_mastermind.js'),
    '--mode', 'critique', '--help',
  ], { env: { ...process.env, OPENCLAW_MEMO_CRITIQUE: '0' } });
  // Either dispatches successfully (returning 0) or prints usage; what we
  // disallow is "unknown mode" error.
  const combined = (result.stdout?.toString() || '') + (result.stderr?.toString() || '');
  assert.ok(!combined.includes('unknown mode'),
    `should recognize 'critique' mode. Got: ${combined.slice(0, 500)}`);
});
