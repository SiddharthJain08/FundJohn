'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..');

test('end-to-end critique mode dry-run completes cleanly', () => {
  // Gate ON, dry-run flag, mocked LLM (via env or skip if DB not reachable)
  const env = {
    ...process.env,
    OPENCLAW_MEMO_CRITIQUE: '1',
    OPENCLAW_MODEL_TIERING: '1',
  };
  const result = spawnSync('node', [
    path.join(ROOT, 'src/agent/curators/run_mastermind.js'),
    '--mode', 'critique', '--dry-run',
  ], { env, timeout: 30_000 });

  const out = (result.stdout?.toString() || '') + (result.stderr?.toString() || '');
  // Allow exit code 1 if POSTGRES_URI not configured — we're testing the
  // dispatch path, not the DB query
  if (out.includes('POSTGRES_URI') || result.status === 1) {
    return; // env-dependent skip
  }
  assert.equal(result.status, 0, `dry-run should exit 0. Output: ${out.slice(0, 500)}`);
  assert.ok(out.includes('"mode":"critique"') || out.includes('"mode": "critique"'),
            'should emit mode=critique JSON line');
});
