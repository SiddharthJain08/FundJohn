'use strict';
/**
 * tests/test_process_guard.test.js — an unhandled 'error' event must not end
 * the process (2026-10-27: it did, and the 15:00 ET compute tick was lost).
 * Run: node --test tests/test_process_guard.test.js
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

const GUARD = path.resolve(__dirname, '..', 'src', 'lib', 'process_guard.js');

function runChild(body) {
  return spawnSync(process.execPath, ['-e', body], { encoding: 'utf8', timeout: 10000 });
}

test('process survives an unhandled EventEmitter error once the guard is installed', () => {
  const r = runChild(`
    const { installUncaughtExceptionGuard } = require(${JSON.stringify(GUARD)});
    const alerts = [];
    installUncaughtExceptionGuard({ log: () => {}, alert: (m) => { alerts.push(m); return Promise.resolve(); } });
    const { EventEmitter } = require('node:events');
    setImmediate(() => new EventEmitter().emit('error', new Error('socket hang up')));
    setTimeout(() => { console.log('ALIVE alerts=' + alerts.length); process.exit(0); }, 100);
  `);
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /ALIVE alerts=1/);
});

test('without the guard the same error ends the process (control)', () => {
  const r = runChild(`
    const { EventEmitter } = require('node:events');
    setImmediate(() => new EventEmitter().emit('error', new Error('socket hang up')));
    setTimeout(() => { console.log('ALIVE'); process.exit(0); }, 100);
  `);
  assert.notEqual(r.status, 0);
  assert.doesNotMatch(r.stdout, /ALIVE/);
});

test('a crash loop still exits after maxPerMinute exceptions', () => {
  const r = runChild(`
    const { installUncaughtExceptionGuard } = require(${JSON.stringify(GUARD)});
    installUncaughtExceptionGuard({ log: () => {}, maxPerMinute: 3 });
    const { EventEmitter } = require('node:events');
    let n = 0;
    const tick = () => { n++; new EventEmitter().emit('error', new Error('again ' + n)); };
    setImmediate(tick); setTimeout(tick, 5); setTimeout(tick, 10);
    setTimeout(() => { console.log('STILL ALIVE'); process.exit(0); }, 100);
  `);
  assert.equal(r.status, 1);
  assert.doesNotMatch(r.stdout, /STILL ALIVE/);
});
