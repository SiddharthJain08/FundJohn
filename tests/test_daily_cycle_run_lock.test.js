'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const fs       = require('node:fs');

const ROOT = path.resolve(__dirname, '..');

test('daily-cycle.js uses the shared run_lock module, not its own key', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src/agent/graphs/daily-cycle.js'), 'utf8');
  assert.ok(!src.includes('engine:run_lock:'), 'legacy engine:run_lock key must be gone');
  assert.ok(src.includes("require('../../lib/run_lock')"), 'must require the shared module');
  assert.ok(src.includes('runLock.acquire('), 'must acquire via the shared module');
  assert.ok(src.includes('runLock.release('), 'release must be value-checked via the module');
  assert.ok(!/r\.del\(key\)/.test(src), 'no unconditional del of the lock key');
});

test('daily-cycle.js publishes the held lock for per-step renewal (QD E1 controller ruling)', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src/agent/graphs/daily-cycle.js'), 'utf8');
  assert.ok(src.includes('runLock.setCurrent('), 'must publish the held lock via setCurrent');
  assert.ok(src.includes('runLock.clearCurrent('), 'must clear the published lock on release');
});

test('daily_cycle_helpers.js renews the shared lock before spawning each step', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'), 'utf8');
  assert.ok(src.includes("require('../../lib/run_lock')"), 'must require the shared module');
  assert.ok(src.includes('runLock.renewCurrent('), 'must renew via the shared module before spawning');
});

test('cron-schedule.js reads the shared lock key for the budget-resume gate', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src/engine/cron-schedule.js'), 'utf8');
  assert.ok(!src.includes('pipeline:running:'), 'legacy pipeline:running key must be gone');
  assert.ok(src.includes("require('../lib/run_lock')"), 'must require the shared module');
  assert.ok(src.includes('runLock.lockKey('), 'must build the key from the shared module');
});
