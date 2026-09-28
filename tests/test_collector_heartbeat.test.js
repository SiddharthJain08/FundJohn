'use strict';

/**
 * tests/test_collector_heartbeat.test.js
 *
 * QD collect-wedge fix (2026-09-28): the daily-cycle stdout-idle watchdog
 * (src/agent/graphs/daily_cycle_helpers.js, default 600s) SIGTERMed the
 * `collect` step mid earnings-calendar walk (2026-09-28T20:28:01Z, rc=125,
 * 780631ms in — see logs/daily_cycle_steps_2026-09-28.log ~L418-477) because
 * that phase — and the insider/fundamentals per-ticker walks — only report
 * progress through tickProgress() (Discord-only) or store.logRun()
 * (Postgres), never stdout. Pins _makeHeartbeat()'s cadence (>=100 ticks OR
 * >=60s wall time, whichever first) with a fake clock — no network, no real
 * timers, no collector run, no master writes.
 *
 * Run: node --test tests/test_collector_heartbeat.test.js
 */

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const collector = require(path.join(path.resolve(__dirname, '..'), 'src/pipeline/collector.js'));

function fakeClock(startMs = 0) {
  let t = startMs;
  return { now: () => t, advance: (ms) => { t += ms; } };
}

test('_makeHeartbeat emits on the 100-tick cadence before 60s elapses', () => {
  // Mirrors the earnings-calendar walk's shape (fast, many ticks, well under
  // the 60s wall-clock budget between emissions).
  const clock = fakeClock();
  const emitted = [];
  const hb = collector._makeHeartbeat('📅 Earnings calendar', 500, {
    everyN: 100, everyMs: 60_000, emit: (m) => emitted.push(m), now: clock.now,
  });
  for (let i = 0; i < 250; i++) {
    clock.advance(10); // 10ms/tick — 250 ticks = 2.5s, well under 60s
    hb.tick(i);
  }
  // Fires at i=100 and i=200 (i - lastI >= 100); tick(0) never fires (0 < 100
  // ticks and 0s elapsed).
  assert.equal(emitted.length, 2);
  assert.match(emitted[0], /^📅 Earnings calendar: 100\/500 \(elapsed 1s\)$/);
  assert.match(emitted[1], /^📅 Earnings calendar: 200\/500 \(elapsed 2s\)$/);
});

test('_makeHeartbeat emits on the 60s wall-clock cadence even with < 100 ticks', () => {
  // Mirrors the insider walk's shape (throttled ~2.1s/ticker via FMP_INTERVAL
  // + sleep, so 100 ticks would take > 3 minutes — the wall-clock cadence
  // must fire well before the tick-count cadence ever could).
  const clock = fakeClock();
  const emitted = [];
  const hb = collector._makeHeartbeat('📋 Insider walk', 30, {
    everyN: 100, everyMs: 60_000, emit: (m) => emitted.push(m), now: clock.now,
  });
  for (let i = 0; i < 30; i++) {
    clock.advance(2100); // 2.1s/tick
    hb.tick(i);
  }
  assert.ok(emitted.length >= 1, 'expected at least one wall-clock heartbeat within 30 throttled ticks');
  assert.match(emitted[0], /^📋 Insider walk: \d+\/30 \(elapsed \d+s\)$/);
  // The first emission must land at/after the 60s budget, not before.
  const firstElapsed = Number(emitted[0].match(/elapsed (\d+)s/)[1]);
  assert.ok(firstElapsed >= 60, `first heartbeat fired at ${firstElapsed}s, expected >= 60s`);
});

test('_makeHeartbeat stays silent between cadences (no per-tick spam)', () => {
  const clock = fakeClock();
  const emitted = [];
  const hb = collector._makeHeartbeat('💹 Fundamentals', 1000, {
    everyN: 100, everyMs: 60_000, emit: (m) => emitted.push(m), now: clock.now,
  });
  for (let i = 0; i < 99; i++) {
    clock.advance(10);
    hb.tick(i);
  }
  assert.equal(emitted.length, 0);
});

test('_makeHeartbeat defaults (everyN=100, everyMs=60000) match the cadence the brief specifies', () => {
  const clock = fakeClock();
  const emitted = [];
  // No everyN/everyMs override — exercise the real production defaults.
  // Fast ticks (10ms each), same shape as the 100-tick-cadence test above, so
  // the 100-tick threshold trips well before the 60s wall-clock one could.
  const hb = collector._makeHeartbeat('📅 Earnings calendar', 5000, {
    emit: (m) => emitted.push(m), now: clock.now,
  });
  for (let i = 0; i < 250; i++) {
    clock.advance(10);
    hb.tick(i);
  }
  assert.equal(emitted.length, 2);
  assert.match(emitted[0], /^📅 Earnings calendar: 100\/5000 \(elapsed 1s\)$/);
  assert.match(emitted[1], /^📅 Earnings calendar: 200\/5000 \(elapsed 2s\)$/);
});
