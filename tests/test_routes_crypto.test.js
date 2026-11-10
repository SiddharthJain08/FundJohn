'use strict';
// tests/test_routes_crypto.test.js — /api/crypto router + SSE event watcher.
// Handlers are exercised through a tiny in-process Express app on an
// ephemeral port (app.listen(0)) with global fetch; the DB `query` and
// `runAlpaca` are injected via router._setQueryForTests / _setRunAlpacaForTests.
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

test('positions normalises BTCUSD and BTC/USD to BTC-USD via the shared alpaca CLI wrapper', async () => {
  let args;
  router._setRunAlpacaForTests(async (a, opts) => {
    args = { a, opts };
    return { ok: true, exit_code: 0, payload: [
      { symbol: 'BTCUSD', qty: '1', market_value: '100', unrealized_pl: '1', unrealized_plpc: '0.01', asset_class: 'crypto' },
      { symbol: 'ETH/USD', qty: '2', market_value: '50', unrealized_pl: '0', unrealized_plpc: '0' },
      { symbol: 'ZZZUSD', qty: '2' },
    ] };
  });
  const { body } = await get('/positions');
  assert.deepEqual(args.a.slice(0, 2), ['position', 'list']);
  assert.equal(args.opts.timeout, 15000);
  assert.deepEqual(body.rows.map((r) => r.ticker), ['BTC-USD', 'ETH-USD', null]);
  router._setRunAlpacaForTests(async () => ({ ok: false, exit_code: 1, stderr: '', payload: null,
    error: { error: 'no alpaca', status: null } }));
  const f = await get('/positions');
  assert.equal(f.body.available, false);
  assert.equal(f.body.reason, 'error');
  assert.match(f.body.error, /no alpaca/);
  router._setRunAlpacaForTests(null);
});

test('positions binary honours ALPACA_CLI_BIN through alpaca_cli.js', () => {
  const src = require('fs').readFileSync(require('path').join(__dirname, '../src/channels/api/routes_crypto.js'), 'utf8');
  assert.doesNotMatch(src, /\/root\/go\/bin\/alpaca/);  // no hard-coded binary left in the route
  assert.match(src, /require\('\.\/alpaca_cli'\)/);
  const { ALPACA_CLI } = require('../src/channels/api/alpaca_cli');
  assert.equal(ALPACA_CLI, process.env.ALPACA_CLI_BIN || '/root/go/bin/alpaca');
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
    assert.equal(body.breaker.status, 'off');
    assert.deepEqual(body.breaker.thresholds, { sleeve_dd: -0.08, position_dd: -0.15, blackout_h: 3 });
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

test('risk breaker: status tri-state from env, evals + hold_flat from DB', async () => {
  const keys = ['OPENCLAW_CRYPTO_BREAKER_TICK', 'OPENCLAW_CRYPTO_BREAKER', 'OPENCLAW_CRYPTO_BREAKER_SLEEVE_DD'];
  const saved = keys.map((k) => process.env[k]);
  try {
    router._setQueryForTests(async (sql) => {
      if (/pipeline_config/.test(sql)) return { rows: [{ value: '{"since":"2026-11-10T10:00:00Z","scope":"sleeve"}' }] };
      if (/crypto_breaker_evals/.test(sql)) return { rows: [{ ts_utc: '2026-11-10T10:05:00Z', mode: 'shadow', action: 'none' }] };
      return { rows: [] };
    });
    delete process.env.OPENCLAW_CRYPTO_BREAKER_TICK; delete process.env.OPENCLAW_CRYPTO_BREAKER;
    let { body } = await get('/risk');
    assert.equal(body.breaker.status, 'off');
    assert.equal(body.breaker.tick_enabled, 'unset');
    assert.equal(body.breaker.hold_flat.scope, 'sleeve');
    assert.equal(body.breaker.last_evals.length, 1);
    process.env.OPENCLAW_CRYPTO_BREAKER_TICK = '1';
    process.env.OPENCLAW_CRYPTO_BREAKER_SLEEVE_DD = '-0.05';
    ({ body } = await get('/risk'));
    assert.equal(body.breaker.status, 'shadow');
    assert.equal(body.breaker.thresholds.sleeve_dd, -0.05);
    process.env.OPENCLAW_CRYPTO_BREAKER = '1';
    ({ body } = await get('/risk'));
    assert.equal(body.breaker.status, 'armed');
    assert.equal(body.breaker.armed, '1');
  } finally {
    keys.forEach((k, i) => { if (saved[i] === undefined) delete process.env[k]; else process.env[k] = saved[i]; });
  }
});

test('risk breaker: DB error -> 200 with hold_flat null, last_evals []', async () => {
  router._setQueryForTests(async () => { throw new Error('boom'); });
  const { status, body } = await get('/risk');
  assert.equal(status, 200);
  assert.equal(body.breaker.hold_flat, null);
  assert.deepEqual(body.breaker.last_evals, []);
});

test('risk breaker: {sleeve, tickers} mirror -> hold_flat = sleeve, hold_tickers = dict; legacy + empty shapes', async () => {
  const mirror = (v) => router._setQueryForTests(async (sql) => {
    if (/pipeline_config/.test(sql)) return { rows: [{ value: v }] };
    return { rows: [] };
  });
  mirror('{"sleeve":{"since":"2026-11-10T10:00:00Z","scope":"sleeve"},"tickers":{"ETH-USD":{"since":"x","ticker":"ETH-USD"}}}');
  let { body } = await get('/risk');
  assert.equal(body.breaker.hold_flat.scope, 'sleeve');
  assert.deepEqual(Object.keys(body.breaker.hold_tickers), ['ETH-USD']);
  mirror({ sleeve: null, tickers: { 'SOL-USD': { since: 'y' } } });   // jsonb object
  ({ body } = await get('/risk'));
  assert.equal(body.breaker.hold_flat, null);
  assert.deepEqual(Object.keys(body.breaker.hold_tickers), ['SOL-USD']);
  mirror('{"sleeve":null,"tickers":{}}');   // empty state is NOT a hold
  ({ body } = await get('/risk'));
  assert.equal(body.breaker.hold_flat, null);
  assert.deepEqual(body.breaker.hold_tickers, {});
  mirror('');
  ({ body } = await get('/risk'));
  assert.equal(body.breaker.hold_flat, null);
  assert.deepEqual(body.breaker.hold_tickers, {});
});

test('risk breaker: DD thresholds outside (-1, 0) fall back to the defaults (mirrors Python)', async () => {
  const keys = ['OPENCLAW_CRYPTO_BREAKER_SLEEVE_DD', 'OPENCLAW_CRYPTO_BREAKER_POSITION_DD'];
  const saved = keys.map((k) => process.env[k]);
  router._setQueryForTests(async () => ({ rows: [] }));
  try {
    for (const [v, want] of [['-8', -0.08], ['0.08', -0.08], ['0', -0.08], ['-1', -0.08], ['-0.05', -0.05], ['abc', -0.08]]) {
      process.env.OPENCLAW_CRYPTO_BREAKER_SLEEVE_DD = v;
      process.env.OPENCLAW_CRYPTO_BREAKER_POSITION_DD = v;
      const { body } = await get('/risk');
      assert.equal(body.breaker.thresholds.sleeve_dd, want, v);
      assert.equal(body.breaker.thresholds.position_dd, want === -0.08 ? -0.15 : want, v);
    }
  } finally {
    keys.forEach((k, i) => { if (saved[i] === undefined) delete process.env[k]; else process.env[k] = saved[i]; });
  }
});

// ── risk.sizing (Phase 3 Task 9) ────────────────────────────────────────────
function cfgQuery(cfg) {
  return async (sql) => {
    if (/pipeline_config/.test(sql) && /ANY/.test(sql)) {
      return { rows: Object.entries(cfg).map(([key, value]) => ({ key, value })) };
    }
    return { rows: [] };
  };
}
async function riskSizing(cfg, flagVal) {
  const saved = process.env.OPENCLAW_CRYPTO_POOLED_SIZING;
  try {
    if (flagVal === undefined) delete process.env.OPENCLAW_CRYPTO_POOLED_SIZING; else process.env.OPENCLAW_CRYPTO_POOLED_SIZING = flagVal;
    router._setQueryForTests(cfgQuery(cfg));
    const { status, body } = await get('/risk');
    assert.equal(status, 200);
    return body.sizing;
  } finally {
    if (saved === undefined) delete process.env.OPENCLAW_CRYPTO_POOLED_SIZING; else process.env.OPENCLAW_CRYPTO_POOLED_SIZING = saved;
  }
}
const SCHEMA3 = {
  schema: 3, rf_source: 'macro',
  benchmarks: {
    SPY: { as_of: '2026-11-09', by_regime: { LOW_VOL: { 1: 0.8 } } },
    'BTC-USD': { as_of: '2026-11-09', start: '2018-01-01', horizons: [1, 2],
      by_regime: { LOW_VOL: { 1: 0.91, 2: 0.5 }, TRANSITIONING: { 1: 0.42 }, HIGH_VOL: { 1: -0.1 }, CRISIS: { 1: 1.5, 2: 1.0 } } },
  },
};

test('risk sizing: schema-3 cache -> four BTC numbers, horizon, caps, last line, shadow by default', async () => {
  const sz = await riskSizing({
    benchmark_regime_sharpe: JSON.stringify(SCHEMA3), benchmark_horizon_days: '1',
    benchmark_max_nav_frac_crypto: '0.3', crypto_gross_max_nav_frac: '0.6', crypto_sizing_last_line: 'crypto_sizing: shadow x',
  });
  assert.equal(sz.status, 'shadow');
  assert.equal(sz.flag, 'unset');
  assert.deepEqual(sz.s_m_by_regime, { LOW_VOL: 0.91, TRANSITIONING: 0.42, HIGH_VOL: -0.1, CRISIS: 1.5 });
  assert.equal(sz.horizon, 1);
  assert.equal(sz.s_m_as_of, '2026-11-09');
  assert.equal(sz.bench_cap_nav, 0.3);
  assert.equal(sz.gross_cap_nav, 0.6);
  assert.equal(sz.last_shadow_line, 'crypto_sizing: shadow x');
});

test('risk sizing: horizon 2 picks the inner key; object-valued JSONB works', async () => {
  const sz = await riskSizing({ benchmark_regime_sharpe: SCHEMA3, benchmark_horizon_days: 2 });
  assert.equal(sz.horizon, 2);
  assert.equal(sz.s_m_by_regime.LOW_VOL, 0.5);
  assert.equal(sz.s_m_by_regime.TRANSITIONING, null);
  assert.equal(sz.s_m_by_regime.CRISIS, 1.0);
});

test('risk sizing: legacy schema-2 SPY-only payload -> nulls for BTC', async () => {
  const sz = await riskSizing({ benchmark_regime_sharpe: { schema: 2, benchmark: 'SPY', as_of: '2026-11-09', by_regime: { LOW_VOL: { 1: 0.8 } } } });
  assert.deepEqual(sz.s_m_by_regime, { LOW_VOL: null, TRANSITIONING: null, HIGH_VOL: null, CRISIS: null });
  assert.equal(sz.s_m_as_of, null);
  // legacy payload that IS BTC maps to benchmarks['BTC-USD']
  const sz2 = await riskSizing({ benchmark_regime_sharpe: { schema: 2, benchmark: 'BTC-USD', as_of: 'd', by_regime: { LOW_VOL: { 1: 0.3 } } } });
  assert.equal(sz2.s_m_by_regime.LOW_VOL, 0.3);
});

test('risk sizing: flag =1 -> apply; unset/0 -> shadow', async () => {
  assert.equal((await riskSizing({}, '1')).status, 'apply');
  assert.equal((await riskSizing({}, '0')).status, 'shadow');
  assert.equal((await riskSizing({})).status, 'shadow');
});

test('risk sizing: missing keys / garbage / DB error -> defaults and nulls, no throw', async () => {
  let sz = await riskSizing({});
  assert.equal(sz.horizon, 1);
  assert.equal(sz.bench_cap_nav, 0.25);
  assert.equal(sz.gross_cap_nav, 0.5);
  assert.equal(sz.last_shadow_line, null);
  assert.deepEqual(Object.values(sz.s_m_by_regime), [null, null, null, null]);
  sz = await riskSizing({ benchmark_regime_sharpe: '{not json', benchmark_horizon_days: 'zz', benchmark_max_nav_frac_crypto: 'x' });
  assert.equal(sz.horizon, 1);
  assert.equal(sz.bench_cap_nav, 0.25);
  assert.deepEqual(Object.values(sz.s_m_by_regime), [null, null, null, null]);
  router._setQueryForTests(async () => { throw new Error('db down'); });
  const { status, body } = await get('/risk');
  assert.equal(status, 200);
  assert.equal(body.sizing.status, 'shadow');
  assert.equal(body.sizing.last_shadow_line, null);
});
