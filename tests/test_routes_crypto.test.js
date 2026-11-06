'use strict';
// tests/test_routes_crypto.test.js — /api/crypto router + SSE event watcher.
// Handlers are exercised through a tiny in-process Express app on an
// ephemeral port (app.listen(0)) with global fetch; the DB `query` and
// `execFile` are injected via router._setQueryForTests / _setExecFileForTests.
const { test, beforeEach, after } = require('node:test');
const assert = require('node:assert/strict');
const express = require('express');
const router = require('../src/channels/api/routes_crypto');

const app = express();
app.use('/api/crypto', router);
const server = app.listen(0);
const base = () => `http://127.0.0.1:${server.address().port}/api/crypto`;
async function get(p) {
  const r = await fetch(base() + p);
  return { status: r.status, body: await r.json() };
}
after(() => { server.close(); router.stopEventWatcher(); });
beforeEach(() => router._setQueryForTests(async () => ({ rows: [] })));

test('snapshot empty table -> available:false', async () => {
  const { status, body } = await get('/snapshot');
  assert.equal(status, 200);
  assert.equal(body.available, false);
  assert.equal(body.reason, 'no_ticks_yet');
});

test('snapshot groups market row and tickers', async () => {
  const t1 = new Date('2026-11-06T10:00:00Z'), t2 = new Date('2026-11-06T09:00:00Z');
  router._setQueryForTests(async (sql) => {
    if (/crypto_flow_provider_health/.test(sql)) {
      return { rows: [{ provider: 'fng', ts_utc: t1, ok: true, rows: 1, latency_ms: 5, err: null, skipped: false }] };
    }
    return { rows: [
      { ticker: 'ETH-USD', ts_utc: t2, features: { whale_score: 3 } },
      { ticker: '__MARKET__', ts_utc: t1, features: { fear_greed: 42 } },
      { ticker: 'BTC-USD', ts_utc: t2, features: { whale_score: 9 } },
      { ticker: 'AAVE-USD', ts_utc: t2, features: { whale_score: 1 } },
    ] };
  });
  const { body } = await get('/snapshot');
  assert.equal(body.available, true);
  assert.equal(body.market.fear_greed, 42);
  assert.equal(new Date(body.ts_utc).toISOString(), t1.toISOString());
  assert.deepEqual(body.tickers.map((t) => t.ticker), ['BTC-USD', 'AAVE-USD', 'ETH-USD']);
  assert.equal(body.tickers[0].role, 'benchmark');
  assert.equal(body.tickers[0].whale_score, 9);
  assert.equal(body.provider_health[0].provider, 'fng');
});

test('history clamps days to 1..30 and casts numbers', async () => {
  const seen = [];
  router._setQueryForTests(async (sql, params) => {
    seen.push(params);
    return { rows: [{ ts_utc: 't', exch_netflow_usd_24h: '1.5', oi_usd: null, funding_8h: '0.01', whale_score: '7', price_usd: '100' }] };
  });
  await get('/history/BTC-USD?days=999');
  await get('/history/BTC-USD?days=0');
  await get('/history/BTC-USD?days=abc');
  const { body } = await get('/history/BTC-USD?days=5');
  assert.deepEqual(seen.map((p) => p[1]), [30, 1, 7, 5]);
  assert.equal(body.rows[0].exch_netflow_usd_24h, 1.5);
  assert.equal(body.rows[0].oi_usd, null);
  const bad = await get('/history/btc;drop');
  assert.equal(bad.body.reason, 'bad_ticker');
});

test('events clamps limit and never throws on db error', async () => {
  const seen = [];
  router._setQueryForTests(async (sql, params) => { seen.push(params[0]); return { rows: [{ event_key: 'a' }] }; });
  await get('/events?limit=9999');
  await get('/events?limit=0');
  await get('/events');
  assert.deepEqual(seen, [200, 1, 50]);
  router._setQueryForTests(async () => { throw new Error('boom'); });
  const { status, body } = await get('/events');
  assert.equal(status, 200);
  assert.equal(body.available, false);
  assert.equal(body.reason, 'error');
  assert.match(body.error, /boom/);
});

test('positions normalises BTCUSD and BTC/USD to BTC-USD', async () => {
  let args;
  router._setExecFileForTests((bin, a, opts, cb) => {
    args = { bin, a, opts };
    cb(null, JSON.stringify([
      { symbol: 'BTCUSD', qty: '1', market_value: '100', unrealized_pl: '1', unrealized_plpc: '0.01', asset_class: 'crypto' },
      { symbol: 'ETH/USD', qty: '2', market_value: '50', unrealized_pl: '0', unrealized_plpc: '0' },
      { symbol: 'ZZZUSD', qty: '2' },
    ]), '');
  });
  const { body } = await get('/positions');
  assert.equal(args.bin, '/root/go/bin/alpaca');
  assert.equal(args.opts.timeout, 15000);
  assert.deepEqual(body.rows.map((r) => r.ticker), ['BTC-USD', 'ETH-USD', null]);
  router._setExecFileForTests((b, a, o, cb) => cb(new Error('no alpaca')));
  const f = await get('/positions');
  assert.equal(f.body.available, false);
  assert.equal(f.body.reason, 'error');
});

test('providers degraded needs three consecutive failures', async () => {
  const mk = (provider, oks) => oks.map((ok, i) => ({ provider, ts_utc: new Date(1e12 - i * 1000), ok, rows: 0, latency_ms: 1, err: ok ? null : 'x', skipped: false, rn: i + 1 }));
  router._setQueryForTests(async () => ({ rows: [
    ...mk('bad', [false, false, false]),
    ...mk('flaky', [false, false, true]),
    ...mk('young', [false, false]),
  ] }));
  const { body } = await get('/providers');
  assert.deepEqual(body.degraded, ['bad']);
  assert.equal(body.providers.length, 3);
  assert.equal(body.providers.find((p) => p.provider === 'flaky').ok, false);
});

test('risk flags reflect env', async () => {
  const keys = ['OPENCLAW_CRYPTO_FLOWS', 'OPENCLAW_CRYPTO_BREAKER', 'OPENCLAW_CRYPTO_POOLED_SIZING', 'OPENCLAW_CRYPTO_WHALE_MIN_USD'];
  const saved = keys.map((k) => process.env[k]);
  try {
    process.env.OPENCLAW_CRYPTO_FLOWS = '1';
    process.env.OPENCLAW_CRYPTO_BREAKER = '0';
    delete process.env.OPENCLAW_CRYPTO_POOLED_SIZING;
    process.env.OPENCLAW_CRYPTO_WHALE_MIN_USD = '2000000';
    const { body } = await get('/risk');
    assert.deepEqual(body.flags, { CRYPTO_FLOWS: '1', CRYPTO_BREAKER: '0', CRYPTO_POOLED_SIZING: 'unset' });
    assert.equal(body.params.whale_min_usd, 2000000);
    assert.equal(body.params.large_print_usd, 250000);
    assert.equal(body.breaker.status, 'pending_phase_2');
    assert.equal(body.sizing.gross_cap_nav, 0.5);
  } finally {
    keys.forEach((k, i) => { if (saved[i] === undefined) delete process.env[k]; else process.env[k] = saved[i]; });
  }
});

test('watcher broadcasts only on advance', async () => {
  let max = new Date('2026-11-06T10:00:00Z');
  const sent = [];
  router._setQueryForTests(async (sql) => {
    if (/MAX\(ts_utc\)/i.test(sql)) return { rows: [{ ts: max }] };
    return { rows: [{ event_key: 'k', ts_utc: max }] };
  });
  const timer = router.startEventWatcher((m) => sent.push(m), { intervalMs: 20 });
  await new Promise((r) => setTimeout(r, 90));
  assert.equal(sent.length, 0, 'first poll only primes the watermark');
  max = new Date('2026-11-06T10:05:00Z');
  await new Promise((r) => setTimeout(r, 90));
  router.stopEventWatcher();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, 'crypto_event');
  assert.equal(sent[0].rows.length, 1);
  assert.ok(timer);
});

test('watcher broadcasts first event after empty start', async () => {
  let max = null;
  const sent = [];
  router._setQueryForTests(async (sql) => {
    if (/MAX\(ts_utc\)/i.test(sql)) return { rows: [{ ts: max }] };
    return { rows: [{ event_key: 'first', ts_utc: max }] };
  });
  router.startEventWatcher((m) => sent.push(m), { intervalMs: 20 });
  await new Promise((r) => setTimeout(r, 70));
  assert.equal(sent.length, 0);
  max = new Date('2026-11-06T11:00:00Z');
  await new Promise((r) => setTimeout(r, 90));
  router.stopEventWatcher();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].rows[0].event_key, 'first');
});

test('watcher skips ticks while a poll is in flight', async () => {
  let calls = 0;
  let release;
  router._setQueryForTests(() => { calls += 1; return new Promise((r) => { release = () => r({ rows: [] }); }); });
  router.startEventWatcher(() => {}, { intervalMs: 10 });
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(calls, 1);
  release();
  router.stopEventWatcher();
});
