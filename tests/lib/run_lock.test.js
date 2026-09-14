'use strict';

const { test }  = require('node:test');
const assert    = require('node:assert/strict');
const path      = require('node:path');
const fs        = require('node:fs');

const ROOT     = path.resolve(__dirname, '..', '..');
const runLock  = require(path.join(ROOT, 'src/lib/run_lock.js'));

const DATE = '2026-09-14';
const KEY  = `pipeline:run_lock:${DATE}`;

// NOTE: extends the brief's FakeRedis with an `eval` method. This is the
// Redis client method name for running a server-side Lua script via the
// Redis EVAL command (ioredis: `redis.eval(script, numkeys, ...args)`) —
// NOT JavaScript's global `eval()`; no arbitrary/untrusted code is ever
// executed here. run_lock.py's fixed takeover is a Lua GET+SET
// compare-and-swap (see run_lock.py `_TAKEOVER_CAS_SCRIPT` and
// tests/lib/test_run_lock.py's own FakeRedis, which already carries this
// same method) — a blind SET on takeover would let two same-host racers
// both "win" a dead-PID lock. The JS twin mirrors that CAS, so its fake
// must be able to emulate one too.
class FakeRedis {
  constructor(store = {}) { this.store = { ...store }; this.ttls = {}; this.calls = []; }
  async set(k, v, ...flags) {
    this.calls.push(['set', k, v, ...flags]);
    const nx = flags.includes('NX');
    if (nx && Object.prototype.hasOwnProperty.call(this.store, k)) return null;
    const exAt = flags.indexOf('EX');
    if (exAt >= 0) this.ttls[k] = flags[exAt + 1];
    this.store[k] = v;
    return 'OK';
  }
  async get(k) { return Object.prototype.hasOwnProperty.call(this.store, k) ? this.store[k] : null; }
  async del(k) { this.calls.push(['del', k]); delete this.store[k]; delete this.ttls[k]; return 1; }
  async expire(k, ttl) {
    this.calls.push(['expire', k, ttl]);
    if (!Object.prototype.hasOwnProperty.call(this.store, k)) return 0;
    this.ttls[k] = ttl; return 1;
  }
  // Emulates run_lock.js's takeover CAS (a Lua GET+SET in real Redis):
  // writes `next` (with `ttl`) iff the key's CURRENT value still equals
  // `expected`, else leaves the store untouched and returns null.
  async eval(_script, _numkeys, key, expected, next, ttl) {
    this.calls.push(['eval', key, expected, next, ttl]);
    if (this.store[key] === expected) {
      this.store[key] = next;
      this.ttls[key] = Number(ttl);
      return 'OK';
    }
    return null;
  }
}

// Simulates a key whose TTL lapses in the gap between our SET NX and our
// GET: the first NX sees the key as present (busy), but a GET run right
// after finds it already gone. acquire()'s retry SET NX must then succeed.
class FakeRedisVanishingKey extends FakeRedis {
  constructor() { super(); this._nxAttempts = 0; }
  async set(k, v, ...flags) {
    this.calls.push(['set', k, v, ...flags]);
    const nx = flags.includes('NX');
    if (nx) {
      this._nxAttempts += 1;
      if (this._nxAttempts === 1) return null; // pretend busy — holder about to vanish
    }
    const exAt = flags.indexOf('EX');
    if (exAt >= 0) this.ttls[k] = flags[exAt + 1];
    this.store[k] = v;
    return 'OK';
  }
  async get(k) {
    if (this._nxAttempts < 2) return null; // the transient holder is already gone
    return Object.prototype.hasOwnProperty.call(this.store, k) ? this.store[k] : null;
  }
}

// Simulates the key expiring under us, then another launcher's acquire()
// winning the SET NX race in that same gap — renew() must fall through to
// "held by someone else" rather than clobbering that legitimate new owner.
class FakeRedisRenewRace extends FakeRedis {
  constructor(otherValue) { super(); this._other = otherValue; this._nxAttempted = false; }
  async set(k, v, ...flags) {
    this.calls.push(['set', k, v, ...flags]);
    const nx = flags.includes('NX');
    if (nx && !this._nxAttempted) {
      this._nxAttempted = true;
      this.store[k] = this._other;
      this.ttls[k] = 9999;
      return null; // our NX sees it as already present -> fails
    }
    const exAt = flags.indexOf('EX');
    if (exAt >= 0) this.ttls[k] = flags[exAt + 1];
    this.store[k] = v;
    return 'OK';
  }
}

// expire() reports the key as gone regardless of prior state — models the
// TTL lapsing strictly between our GET (which matched our own value) and
// our EXPIRE call. renew() must honor that real return value.
class FakeRedisExpireMisses extends FakeRedis {
  async expire(k, ttl) {
    this.calls.push(['expire', k, ttl]);
    delete this.store[k];
    delete this.ttls[k];
    return 0;
  }
}

// del() blows up — models a Redis connection error on the way out.
// release() must swallow this, never propagate it.
class FakeRedisDeleteRaises extends FakeRedis {
  async del(k) {
    this.calls.push(['del', k]);
    throw new Error('redis unavailable');
  }
}

test('both twins read the key prefix from the same JSON file', () => {
  const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/lib/run_lock_key.json'), 'utf8'));
  assert.equal(cfg.prefix, 'pipeline:run_lock');
  assert.equal(runLock.KEY_PREFIX, cfg.prefix);
  assert.equal(runLock.lockKey(DATE), KEY);
  assert.equal(runLock.TTL_SLACK_SECONDS, cfg.ttl_slack_seconds);
  assert.equal(runLock.LOCK_BUSY_RC, 75);
});

test('value format matches the Python twin exactly', () => {
  const v = runLock.makeValue({ pid: 4242, host: 'vps1', startedAt: '2026-09-14T10:00:00+00:00' });
  assert.equal(v, 'vps1:4242:2026-09-14T10:00:00+00:00');
  assert.deepEqual(runLock.parseValue(v), { host: 'vps1', pid: 4242, startedAt: '2026-09-14T10:00:00+00:00' });
  assert.equal(runLock.parseValue('1'), null);
  assert.equal(runLock.parseValue(''), null);
});

test('ttlFor = timeout + 120 with a 300s floor', () => {
  assert.equal(runLock.ttlFor(9000), 9120);
  assert.equal(runLock.ttlFor(300), 420);
  assert.equal(runLock.ttlFor(1), runLock.MIN_TTL_SECONDS);
});

test('acquire takes a free lock and records the owned value', async () => {
  const r = new FakeRedis();
  const out = await runLock.acquire(r, DATE, 420, { value: 'vps1:111:T0' });
  assert.equal(out.ok, true);
  assert.equal(r.store[KEY], 'vps1:111:T0');
  assert.equal(r.ttls[KEY], 420);
});

test('acquire refuses a lock held by a live pid on this host', async () => {
  const held = `vps1:${process.pid}:T0`;
  const r = new FakeRedis({ [KEY]: held });
  const out = await runLock.acquire(r, DATE, 420, { value: 'vps1:999:T1', host: 'vps1' });
  assert.equal(out.ok, false);
  assert.equal(out.holder, held);
  assert.equal(r.store[KEY], held);
});

test('acquire takes over a dead pid on this host and logs it', async () => {
  const logs = [];
  const r = new FakeRedis({ [KEY]: 'vps1:999999:T0' });
  const out = await runLock.acquire(r, DATE, 420,
    { value: 'vps1:111:T1', host: 'vps1', log: (m) => logs.push(m) });
  assert.equal(out.ok, true);
  assert.equal(r.store[KEY], 'vps1:111:T1');
  assert.ok(logs.some(m => m.includes('stale lock from pid 999999')), logs.join('|'));
});

test('acquire never touches a lock owned by another host', async () => {
  const r = new FakeRedis({ [KEY]: 'otherbox:999999:T0' });
  const out = await runLock.acquire(r, DATE, 420, { value: 'vps1:111:T1', host: 'vps1' });
  assert.equal(out.ok, false);
  assert.equal(r.store[KEY], 'otherbox:999999:T0');
});

test('acquire refuses on an unparseable holder rather than stealing', async () => {
  const r = new FakeRedis({ [KEY]: '1' });
  const out = await runLock.acquire(r, DATE, 420, { value: 'vps1:111:T1', host: 'vps1' });
  assert.equal(out.ok, false);
  assert.equal(out.holder, '1');
});

test('acquire retries the NX once when the holder vanishes before our GET', async () => {
  const r = new FakeRedisVanishingKey();
  const out = await runLock.acquire(r, DATE, 420, { value: 'vps1:111:T1', host: 'vps1' });
  assert.equal(out.ok, true);
  assert.equal(out.value, 'vps1:111:T1');
  assert.equal(r.store[KEY], 'vps1:111:T1');
  const nxSets = r.calls.filter(c => c[0] === 'set' && c.includes('NX'));
  assert.equal(nxSets.length, 2); // the original attempt + one retry
});

test('takeover is a CAS: two same-host racers through acquire() yield exactly one winner', async () => {
  const r = new FakeRedis({ [KEY]: 'vps1:999999:T0' });
  const myValue = `vps1:${process.pid}:T1`;
  const out1 = await runLock.acquire(r, DATE, 420, { value: myValue, host: 'vps1' });
  assert.equal(out1.ok, true);
  assert.equal(r.store[KEY], myValue);

  // Racer 2 now sees a genuinely live holder (racer 1's own pid) and
  // refuses via the ordinary liveness check.
  const out2 = await runLock.acquire(r, DATE, 420, { value: 'vps1:222:T2', host: 'vps1' });
  assert.equal(out2.ok, false);
  assert.equal(out2.holder, myValue);
  assert.equal(r.store[KEY], myValue);
});

test('takeover CAS is conditional — a losing racer never touches the store', async () => {
  const stale = 'vps1:999999:T0';
  const r = new FakeRedis({ [KEY]: stale });
  const ttl = 420;

  const out1 = await runLock.acquire(r, DATE, ttl, { value: 'vps1:111:T1', host: 'vps1' });
  assert.equal(out1.ok, true);
  assert.equal(r.store[KEY], 'vps1:111:T1');

  // Racer 2's CAS, conditioned on the same stale value both racers
  // observed, now runs against a store racer 1 already moved on.
  const result2 = await r.eval('irrelevant-script-text', 1, KEY, stale, 'vps1:222:T2', ttl);
  assert.ok(!result2);
  assert.equal(r.store[KEY], 'vps1:111:T1'); // untouched by the loser
});

test('renew and release are value-checked', async () => {
  const r = new FakeRedis({ [KEY]: 'vps1:111:T0' });
  assert.equal(await runLock.renew(r, DATE, 'vps1:111:T0', 9120), true);
  assert.equal(r.ttls[KEY], 9120);

  r.store[KEY] = 'vps1:222:T1';                       // taken over
  assert.equal(await runLock.renew(r, DATE, 'vps1:111:T0', 9120), false);
  assert.equal(await runLock.release(r, DATE, 'vps1:111:T0'), false);
  assert.equal(r.store[KEY], 'vps1:222:T1');
  assert.ok(!r.calls.some(c => c[0] === 'del'));

  r.store[KEY] = 'vps1:111:T0';
  assert.equal(await runLock.release(r, DATE, 'vps1:111:T0'), true);
  assert.equal(r.store[KEY], undefined);
});

test('renew re-takes a lock that expired under us', async () => {
  const r = new FakeRedis();
  const ok = await runLock.renew(r, DATE, 'vps1:111:T0', 9120);
  assert.equal(ok, true);
  assert.equal(r.store[KEY], 'vps1:111:T0');
});

test('renew refuses when the key expired and someone else won the retake', async () => {
  const logs = [];
  const r = new FakeRedisRenewRace('vps1:222:T9');
  const ok = await runLock.renew(r, DATE, 'vps1:111:T0', 9120, { log: (m) => logs.push(m) });
  assert.equal(ok, false);
  assert.equal(r.store[KEY], 'vps1:222:T9'); // the real owner's write stands
  assert.ok(logs.some(m => m.includes('held by vps1:222:T9')), logs.join('|'));
});

test('renew returns false when expire finds the key gone', async () => {
  const r = new FakeRedisExpireMisses({ [KEY]: 'vps1:111:T0' });
  const ok = await runLock.renew(r, DATE, 'vps1:111:T0', 9120);
  assert.equal(ok, false);
});

test('release swallows a delete error and returns false', async () => {
  const logs = [];
  const r = new FakeRedisDeleteRaises({ [KEY]: 'vps1:111:T0' });
  const ok = await runLock.release(r, DATE, 'vps1:111:T0', { log: (m) => logs.push(m) });
  assert.equal(ok, false);
  assert.ok(logs.some(m => m.includes('release of')), logs.join('|'));
});

// ── setCurrent / clearCurrent / renewCurrent (QD E1 controller ruling,
// 2026-09-14: this LangGraph twin IS production whenever
// OPENCLAW_LANGGRAPH_ORCHESTRATOR=1, so it must renew before every step). ──

test('renewCurrent is a no-op success when no lock is held', async () => {
  runLock.clearCurrent(); // start clean regardless of prior test state
  assert.equal(await runLock.renewCurrent(9000), true);
});

test('renewCurrent renews the lock set via setCurrent', async () => {
  const r = new FakeRedis({ [KEY]: 'vps1:111:T0' });
  runLock.setCurrent({ r, runDate: DATE, value: 'vps1:111:T0' });
  try {
    const ok = await runLock.renewCurrent(9000);
    assert.equal(ok, true);
    assert.equal(r.ttls[KEY], runLock.ttlFor(9000));
  } finally {
    runLock.clearCurrent();
  }
});

test('renewCurrent reports loss immediately when another owner now holds the key (no retry)', async () => {
  let getCalls = 0;
  const r = { async get() { getCalls += 1; return 'vps1:222:T1'; } }; // someone else's value
  runLock.setCurrent({ r, runDate: DATE, value: 'vps1:111:T0' });
  try {
    const ok = await runLock.renewCurrent(9000);
    assert.equal(ok, false);
    assert.equal(getCalls, 1); // one GET, no retry — a clean mismatch is definitive, not transient
  } finally {
    runLock.clearCurrent();
  }
});

test('renewCurrent retries once after a Redis exception, then reports lost', async () => {
  let calls = 0;
  const throwingRedis = { async get() { calls += 1; throw new Error('ECONNRESET'); } };
  const logs = [];
  runLock.setCurrent({ r: throwingRedis, runDate: DATE, value: 'vps1:111:T0' });
  try {
    const ok = await runLock.renewCurrent(9000, { retryDelayMs: 0, log: (m) => logs.push(m) });
    assert.equal(ok, false);
    assert.equal(calls, 2); // the attempt + one retry
    assert.ok(logs.some(m => m.includes('retrying once')), logs.join('|'));
    assert.ok(logs.some(m => m.includes('treating the lock as lost')), logs.join('|'));
  } finally {
    runLock.clearCurrent();
  }
});

test('renewCurrent succeeds on the retry after one transient exception', async () => {
  let calls = 0;
  const flakyRedis = {
    async get() {
      calls += 1;
      if (calls === 1) throw new Error('ECONNRESET');
      return 'vps1:111:T0';
    },
    async expire() { return 1; },
  };
  runLock.setCurrent({ r: flakyRedis, runDate: DATE, value: 'vps1:111:T0' });
  try {
    const ok = await runLock.renewCurrent(9000, { retryDelayMs: 0 });
    assert.equal(ok, true);
    assert.equal(calls, 2);
  } finally {
    runLock.clearCurrent();
  }
});
