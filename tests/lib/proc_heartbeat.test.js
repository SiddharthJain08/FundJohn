'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const ph   = require(path.join(ROOT, 'src/lib/proc_heartbeat.js'));

// Buffers hset/expire and applies them ALL AT ONCE on exec() — mirrors
// ioredis's multi()/exec() chain closely enough for this test double: ops
// land directly on the parent's hashes/ttls (NOT via the parent's own
// hset()/expire() methods), so `directHsetCalls`/`directExpireCalls` stay a
// clean signal of whether a caller bypassed multi() for bare top-level calls.
class _FakeMulti {
  constructor(parent) { this._parent = parent; this._ops = []; }
  hset(key, obj) { this._ops.push(['hset', key, obj]); return this; }
  expire(key, ttl) { this._ops.push(['expire', key, ttl]); return this; }
  async exec() {
    for (const [op, key, val] of this._ops) {
      if (op === 'hset') this._parent.hashes[key] = { ...(this._parent.hashes[key] || {}), ...val };
      else this._parent.ttls[key] = val;
    }
    this._ops = [];
    return [];
  }
}

class FakeRedis {
  constructor() {
    this.hashes = {}; this.ttls = {};
    this.directHsetCalls = 0; this.directExpireCalls = 0; this.multiCount = 0;
  }
  async hset(key, obj) { this.directHsetCalls++; this.hashes[key] = { ...(this.hashes[key] || {}), ...obj }; return 1; }
  async expire(key, ttl) { this.directExpireCalls++; this.ttls[key] = ttl; return 1; }
  async del(key) { delete this.hashes[key]; delete this.ttls[key]; return 1; }
  multi() { this.multiCount++; return new _FakeMulti(this); }
}

class ExplodingRedis {
  async hset() { throw new Error('redis down'); }
  async expire() { throw new Error('redis down'); }
  async del() { throw new Error('redis down'); }
  multi() { throw new Error('redis down'); }
}

test('key shape matches the Python twin', () => {
  assert.equal(ph.heartbeatKey({ host: 'vps1', pid: 42 }), 'proc:vps1:42');
  assert.ok(ph.heartbeatKey().endsWith(`:${process.pid}`));
  assert.equal(ph.DEFAULT_TTL_SEC, 180);
});

test('rssMb is positive for this process and null for a dead pid', () => {
  assert.ok(ph.rssMb() > 0);
  assert.equal(ph.rssMb(999999), null);
});

test('writeHeartbeat stores every field as a string and sets the TTL', async () => {
  const r = new FakeRedis();
  const key = await ph.writeHeartbeat(r, {
    step: 'fleet:S_x', argv: ['python3', '-m', 'backtest.unified_backtest'],
    pid: 42, host: 'vps1', startedAt: '2026-09-14T10:00:00.000Z', ttlSec: 3900,
  });
  assert.equal(key, 'proc:vps1:42');
  const h = r.hashes[key];
  assert.equal(h.step, 'fleet:S_x');
  assert.equal(h.argv, 'python3 -m backtest.unified_backtest');
  assert.equal(h.host, 'vps1');
  assert.equal(h.pid, '42');
  assert.equal(h.started_at, '2026-09-14T10:00:00.000Z');
  assert.ok(h.updated_at);
  assert.ok('rss_mb' in h);
  assert.ok(Object.values(h).every(v => typeof v === 'string'));
  assert.equal(r.ttls[key], 3900);
});

test('writeHeartbeat never throws when redis is down', async () => {
  assert.equal(await ph.writeHeartbeat(new ExplodingRedis(), { step: 's', pid: 1, host: 'h' }), null);
  await ph.clearHeartbeat(new ExplodingRedis(), { pid: 1, host: 'h' });   // must not throw
  assert.equal(await ph.writeHeartbeat(null, { step: 's' }), null);
});

test('writeHeartbeat runs hset+expire through one multi(), not two bare calls', async () => {
  // Fix round 1, minor item 1: a half-written key (fields with no TTL) must
  // never be observable — hset + expire go through ONE multi/EXEC.
  const r = new FakeRedis();
  await ph.writeHeartbeat(r, { step: 's', pid: 42, host: 'vps1' });
  assert.equal(r.multiCount, 1);
  assert.equal(r.directHsetCalls, 0);
  assert.equal(r.directExpireCalls, 0);
});

test('writeHeartbeat returns null when the multi blows up mid-transaction', async () => {
  class _BlowsUpMidTransaction {
    hset() { return this; }
    expire() { throw new Error('connection dropped mid-transaction'); }
    async exec() { throw new Error('connection dropped mid-transaction'); }
  }
  const r = new FakeRedis();
  r.multi = () => new _BlowsUpMidTransaction();
  assert.equal(await ph.writeHeartbeat(r, { step: 's', pid: 42, host: 'vps1' }), null);
});

test('rssMbOverride writes rss_mb as-is, still through one multi (fix round 1, minor item 5)', async () => {
  // The fleet driver writes a heartbeat BEFORE its child exists — rssMb(pid)
  // would silently sample the DRIVER's own RSS otherwise. The override must
  // land in the SAME multi/EXEC as every other field, not a second
  // round-trip that would leave a window with a misleading value.
  const r = new FakeRedis();
  const key = await ph.writeHeartbeat(r, {
    step: 'fleet:S_x', pid: 42, host: 'vps1', rssMbOverride: '',
  });
  assert.equal(r.hashes[key].rss_mb, '');
  assert.equal(r.multiCount, 1);
  assert.equal(r.directHsetCalls, 0);
});
