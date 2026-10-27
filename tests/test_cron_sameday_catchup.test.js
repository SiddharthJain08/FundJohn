'use strict';
/**
 * tests/test_cron_sameday_catchup.test.js — registration-time same-day
 * compute catch-up decision (2026-10-27: johnbot restarted at 19:00:26Z after
 * an OOM-induced crash, node-cron re-registered after the 15:00 ET tick, no
 * handoff was produced and the 15:55 lane aborted — a lost trading day).
 * Run: node --test tests/test_cron_sameday_catchup.test.js
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');

const { sameDayComputeCatchUpDue, SAMEDAY_CATCHUP_WINDOW } = require('../src/engine/cron-schedule');

const at = (iso) => new Date(iso);

test('window is 15:00–15:45 ET', () => {
  assert.deepEqual(SAMEDAY_CATCHUP_WINDOW, { start: '15:00', end: '15:45' });
});

test('the 2026-10-27 restart (19:01Z = 15:01 EDT, no handoff) is due', () => {
  const r = sameDayComputeCatchUpDue({ now: at('2026-10-27T19:01:09Z'), handoffExists: false });
  assert.equal(r.due, true, r.reason);
  assert.match(r.reason, /Tue 15:01 ET/);
});

test('same minute with a handoff already written is not due', () => {
  const r = sameDayComputeCatchUpDue({ now: at('2026-10-27T19:01:09Z'), handoffExists: true });
  assert.equal(r.due, false);
  assert.match(r.reason, /already exists/);
});

test('one minute before 15:00 ET is not due (the cron tick itself covers it)', () => {
  const r = sameDayComputeCatchUpDue({ now: at('2026-10-27T18:59:00Z'), handoffExists: false });
  assert.equal(r.due, false);
  assert.match(r.reason, /outside/);
});

test('15:45 ET and later is not due (too late for the 15:55 execute lane)', () => {
  assert.equal(sameDayComputeCatchUpDue({ now: at('2026-10-27T19:45:00Z') }).due, false);
  assert.equal(sameDayComputeCatchUpDue({ now: at('2026-10-27T19:50:00Z') }).due, false);
  assert.equal(sameDayComputeCatchUpDue({ now: at('2026-10-27T19:44:59Z') }).due, true);
});

test('weekends are never due', () => {
  const r = sameDayComputeCatchUpDue({ now: at('2026-10-31T19:10:00Z'), handoffExists: false }); // Saturday
  assert.equal(r.due, false);
  assert.match(r.reason, /weekend/);
});

test('EST (after the November clock change) keeps the ET window: 20:10Z = 15:10 EST', () => {
  const r = sameDayComputeCatchUpDue({ now: at('2026-12-01T20:10:00Z'), handoffExists: false });
  assert.equal(r.due, true, r.reason);
  assert.equal(sameDayComputeCatchUpDue({ now: at('2026-12-01T19:10:00Z') }).due, false); // 14:10 EST
});
