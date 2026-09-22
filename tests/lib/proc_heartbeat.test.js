'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const ph   = require(path.join(ROOT, 'src/lib/proc_heartbeat.js'));

class FakeRedis {
  constructor() { this.hashes = {}; this.ttls = {}; }
  async hset(key, obj) { this.hashes[key] = { ...(this.hashes[key] || {}), ...obj }; return 1; }
  async expire(key, ttl) { this.ttls[key] = ttl; return 1; }
  async del(key) { delete this.hashes[key]; delete this.ttls[key]; return 1; }
}

class ExplodingRedis {
  async hset() { throw new Error('redis down'); }
  async expire() { throw new Error('redis down'); }
  async del() { throw new Error('redis down'); }
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
