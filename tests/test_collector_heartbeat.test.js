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
 * (Postgres), never stdout.
 *
 * Two helpers, two shapes of "no stdout":
 *   - _makeHeartbeat()  — the insider/fundamentals JS loops have a real `i`
 *     to tick. Cadence: >=100 ticks OR >=60s wall time, whichever first.
 *   - _withPhasePing()  — the earnings-calendar phase awaits a single
 *     buffered subprocess call with NO observable `i`; this pings on a pure
 *     60s wall-clock interval instead.
 * Both are exercised here with a fake clock / injected fake timers — no
 * network, no real timers (except one short end-to-end check), no collector
 * run, no master writes.
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

// Fake setInterval/clearInterval: the test fires ticks by hand (via
// `fire(id)`) instead of waiting on a real timer, and records every
// clearInterval call so "the ping was torn down" is a plain assertion.
function fakeTimers() {
  let idCounter = 0;
  const registered = new Map(); // id -> { cb, ms }
  const cleared = [];
  return {
    setIntervalFn: (cb, ms) => { const id = ++idCounter; registered.set(id, { cb, ms }); return id; },
    clearIntervalFn: (id) => { cleared.push(id); registered.delete(id); },
    fire: (id) => registered.get(id).cb(),
    registered,
    cleared,
  };
}

// ── _makeHeartbeat — insider / fundamentals (real per-ticker `i`) ───────────

test('_makeHeartbeat (insider walk shape) emits on the 100-tick cadence before 60s elapses', () => {
  // Mirrors a fast-completing insider batch: many ticks, well under the 60s
  // wall-clock budget between emissions.
  const clock = fakeClock();
  const emitted = [];
  const hb = collector._makeHeartbeat('📋 Insider walk', 500, {
    everyN: 100, everyMs: 60_000, emit: (m) => emitted.push(m), now: clock.now,
  });
  for (let i = 0; i < 250; i++) {
    clock.advance(10); // 10ms/tick — 250 ticks = 2.5s, well under 60s
    hb.tick(i);
  }
  // Fires at i=100 and i=200 (i - lastI >= 100); tick(0) never fires (0 < 100
  // ticks and 0s elapsed).
  assert.equal(emitted.length, 2);
  assert.match(emitted[0], /^📋 Insider walk: 100\/500 \(elapsed 1s\)$/);
  assert.match(emitted[1], /^📋 Insider walk: 200\/500 \(elapsed 2s\)$/);
});

test('_makeHeartbeat (insider walk shape) emits on the 60s wall-clock cadence even with < 100 ticks', () => {
  // Mirrors the insider walk's real throttle (~2.1s/ticker via FMP_INTERVAL +
  // sleep), where 100 ticks would take > 3 minutes — the wall-clock cadence
  // must fire well before the tick-count cadence ever could.
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

test('_makeHeartbeat (fundamentals shape) stays silent between cadences (no per-tick spam)', () => {
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

test('_makeHeartbeat (fundamentals shape) defaults (everyN=100, everyMs=60000) match the cadence the brief specifies', () => {
  const clock = fakeClock();
  const emitted = [];
  // No everyN/everyMs override — exercise the real production defaults.
  const hb = collector._makeHeartbeat('💹 Fundamentals', 5000, {
    emit: (m) => emitted.push(m), now: clock.now,
  });
  for (let i = 0; i < 250; i++) {
    clock.advance(10);
    hb.tick(i);
  }
  assert.equal(emitted.length, 2);
  assert.match(emitted[0], /^💹 Fundamentals: 100\/5000 \(elapsed 1s\)$/);
  assert.match(emitted[1], /^💹 Fundamentals: 200\/5000 \(elapsed 2s\)$/);
});

// ── _withPhasePing — earnings calendar (no observable `i`) ──────────────────

test('_withPhasePing (earnings-calendar shape) emits on each ping tick while the wrapped call is pending', async () => {
  const clock  = fakeClock();
  const timers = fakeTimers();
  const emitted = [];
  let resolveFn;
  const fn = () => new Promise((resolve) => { resolveFn = resolve; });

  const promise = collector._withPhasePing('📅 Earnings calendar', fn, {
    everyMs: 60_000, emit: (m) => emitted.push(m), now: clock.now,
    setIntervalFn: timers.setIntervalFn, clearIntervalFn: timers.clearIntervalFn,
  });

  assert.equal(timers.registered.size, 1, 'expected exactly one interval registered');
  const [id] = timers.registered.keys();

  clock.advance(60_000);
  timers.fire(id);
  clock.advance(60_000);
  timers.fire(id);
  assert.equal(emitted.length, 2);
  assert.match(emitted[0], /^📅 Earnings calendar: still walking \(elapsed 60s\)$/);
  assert.match(emitted[1], /^📅 Earnings calendar: still walking \(elapsed 120s\)$/);

  resolveFn('done');
  const result = await promise;
  assert.equal(result, 'done');
  assert.deepEqual(timers.cleared, [id], 'the interval must be cleared once the wrapped call resolves');
});

test('_withPhasePing (earnings-calendar shape) clears the ping even when the wrapped call rejects', async () => {
  const timers = fakeTimers();
  const boom = new Error('boom');
  const fn = () => Promise.reject(boom);

  const promise = collector._withPhasePing('📅 Earnings calendar', fn, {
    everyMs: 60_000, emit: () => {}, now: () => 0,
    setIntervalFn: timers.setIntervalFn, clearIntervalFn: timers.clearIntervalFn,
  });

  await assert.rejects(promise, /boom/);
  assert.equal(timers.cleared.length, 1, 'the interval must be cleared even on rejection (finally path)');
});

test('_withPhasePing wires real setInterval/clearInterval by default and still resolves', async () => {
  // End-to-end check with the REAL default timers (short interval, short
  // wrapped delay) — confirms runEarningsCalendar()'s actual wiring, not
  // just the injected-fake path above. Fast (<100ms), no network.
  const emitted = [];
  const result = await collector._withPhasePing('📅 Earnings calendar', async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
    return 'ok';
  }, { everyMs: 10, emit: (m) => emitted.push(m) });
  assert.equal(result, 'ok');
  assert.ok(emitted.length >= 1, 'expected at least one real-timer ping during the 30ms wrapped call');
});
