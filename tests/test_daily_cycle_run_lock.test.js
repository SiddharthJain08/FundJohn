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

// ── QD wave-1 fix item 2 ─────────────────────────────────────────────────────
// `runDailyCycleGraph` acquires the lock, then ran FOUR awaits (checkpointer
// setup, graph compile, traceBus.startRun, the Discord cycleStart post) before
// entering the try/finally that releases it. A throw in any of them escaped
// the function without ever releasing — leaving a LIVE-PID value in Redis for
// the full TTL (~9120 s; `acquire` only ever takes over a DEAD pid, so no
// takeover was possible) and a stale `_current` handle in this long-lived
// johnbot process. The Python twin then refused every run for that date with
// rc=75 for 2.5 h. This test drives that exact path with a checkpointer that
// throws and pins both halves of the fix: the key is deleted, and no current
// lock handle survives.
//
// Hermetic: a fake `ioredis` and a fake `PostgresSaver` are injected into
// require.cache before daily-cycle.js is loaded, and pipeline_logging /
// traceBus are stubbed the same way tests/test_daily_cycle_graph.test.js does
// it. No real Redis, Postgres, Discord, or subprocess is ever touched.

class FakeRedis {
  constructor() { this.store = {}; this.ttls = {}; this.calls = []; }
  async set(k, v, ...flags) {
    this.calls.push(['set', k]);
    if (flags.includes('NX') && Object.prototype.hasOwnProperty.call(this.store, k)) return null;
    const exAt = flags.indexOf('EX');
    if (exAt >= 0) this.ttls[k] = flags[exAt + 1];
    this.store[k] = v;
    return 'OK';
  }
  async get(k) {
    this.calls.push(['get', k]);
    return Object.prototype.hasOwnProperty.call(this.store, k) ? this.store[k] : null;
  }
  async del(k) { this.calls.push(['del', k]); delete this.store[k]; delete this.ttls[k]; return 1; }
  async expire(k, ttl) { this.calls.push(['expire', k, ttl]); this.ttls[k] = ttl; return 1; }
  // `eval` here is the ioredis client method for the Redis EVAL command
  // (server-side Lua) — NOT JavaScript's global eval(); no code is executed.
  // run_lock.acquire only reaches it on a dead-PID takeover, which this test
  // never triggers, so returning null (CAS lost) is enough.
  async eval() { this.calls.push(['eval']); return null; }
  async quit() { this.calls.push(['quit']); return 'OK'; }
}

test('a throw between the acquire and the graph releases the lock (no leaked LIVE-PID key)', async () => {
  const runLock = require(path.join(ROOT, 'src/lib/run_lock.js'));
  const RUN_DATE = '2026-09-14';
  const KEY = runLock.lockKey(RUN_DATE);

  const IOREDIS_PATH  = require.resolve('ioredis');
  const PGSAVER_PATH  = require.resolve('@langchain/langgraph-checkpoint-postgres');
  const DAILY_PATH    = require.resolve(path.join(ROOT, 'src/agent/graphs/daily-cycle.js'));
  const LOGGING_PATH  = require.resolve(path.join(ROOT, 'src/execution/pipeline_logging.js'));
  const TRACEBUS_PATH = require.resolve(path.join(ROOT, 'src/agent/traceBus.js'));

  const saved = {};
  for (const p of [IOREDIS_PATH, PGSAVER_PATH, DAILY_PATH, LOGGING_PATH, TRACEBUS_PATH]) {
    saved[p] = require.cache[p];
  }
  const savedMemSaver = process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER;
  // MemorySaver mode skips the lock entirely — this test needs the real
  // acquire path, so force the flag off for the duration.
  delete process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER;

  const fake = new FakeRedis();
  require.cache[IOREDIS_PATH] = {
    id: IOREDIS_PATH, filename: IOREDIS_PATH, loaded: true,
    exports: function Redis() { return fake; },
  };
  require.cache[PGSAVER_PATH] = {
    id: PGSAVER_PATH, filename: PGSAVER_PATH, loaded: true,
    exports: {
      PostgresSaver: {
        fromConnString: () => { throw new Error('checkpointer boom'); },
      },
    },
  };
  require.cache[LOGGING_PATH] = {
    id: LOGGING_PATH, filename: LOGGING_PATH, loaded: true,
    exports: {
      feedStart: async () => {}, feedEnd: async () => {}, notifyFailure: async () => {},
      cycleStart: async () => {}, cycleEnd: async () => {}, updateAgentStatus: async () => {},
    },
  };
  require.cache[TRACEBUS_PATH] = {
    id: TRACEBUS_PATH, filename: TRACEBUS_PATH, loaded: true,
    exports: { push: () => {}, startRun: () => {}, endRun: () => {} },
  };
  delete require.cache[DAILY_PATH];

  runLock.clearCurrent();
  try {
    const mod = require(DAILY_PATH);
    await assert.rejects(
      () => mod.runDailyCycleGraph({ runDate: RUN_DATE, reason: 'test' }),
      /checkpointer boom/,
    );
    // The acquire really happened (otherwise this test proves nothing)…
    assert.ok(fake.calls.some(([op, k]) => op === 'set' && k === KEY), 'lock was never acquired');
    // …and the key is gone again — not left behind for the full TTL.
    assert.equal(Object.prototype.hasOwnProperty.call(fake.store, KEY), false);
    assert.ok(fake.calls.some(([op, k]) => op === 'del' && k === KEY), 'lock was never released');
    assert.ok(fake.calls.some(([op]) => op === 'quit'), 'the redis client was never closed');

    // No current handle survives: renewCurrent() with no lock published is a
    // no-op success that touches Redis zero times.
    fake.calls.length = 0;
    assert.equal(await runLock.renewCurrent(600), true);
    assert.deepEqual(fake.calls, []);
  } finally {
    runLock.clearCurrent();
    for (const [p, entry] of Object.entries(saved)) {
      if (entry === undefined) delete require.cache[p];
      else require.cache[p] = entry;
    }
    delete require.cache[DAILY_PATH];
    if (savedMemSaver === undefined) delete process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER;
    else process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER = savedMemSaver;
  }
});
