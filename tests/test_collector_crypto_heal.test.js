'use strict';

/**
 * Crypto daily-bar self-heal window in runMarketPricesNonEquity.
 * Run: node --test tests/test_collector_crypto_heal.test.js
 */

const { test, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const path   = require('node:path');

const root      = path.resolve(__dirname, '..');
const collector = require(path.join(root, 'src/pipeline/collector.js'));
const store     = require(path.join(root, 'src/pipeline/store.js'));

const iso = (d) => new Date(Date.now() + d * 86400_000).toISOString().slice(0, 10);

let fills, msgs, saved, savedEnv;
beforeEach(() => {
  fills = []; msgs = [];
  saved = { getGaps: store.getGaps, logRun: store.logRun, flushPrices: store.flushPrices };
  savedEnv = process.env.OPENCLAW_CRYPTO_BARS_HEAL_DAYS;
  delete process.env.OPENCLAW_CRYPTO_BARS_HEAL_DAYS;
  store.getGaps = async () => [];
  store.logRun = async () => {};
  store.flushPrices = async () => ({ flushed: 0 });
  collector._testHooks.fillMarketTicker = async (category, ticker, from, to) => { fills.push({ category, ticker, from, to }); return 1; };
  collector.setBroadcast((m) => msgs.push(m.message));
});
afterEach(() => {
  Object.assign(store, saved);
  delete collector._testHooks.fillMarketTicker;
  collector.setBroadcast(null);
  if (savedEnv === undefined) delete process.env.OPENCLAW_CRYPTO_BARS_HEAL_DAYS;
  else process.env.OPENCLAW_CRYPTO_BARS_HEAL_DAYS = savedEnv;
});

const HEAL_MSG = (n) => `🌐 Market prices: crypto self-heal window ${n} d (coverage-range gaps cannot see holes)`;

test('crypto covered through today -> ONE fetch for the heal window, not skipped', async () => {
  await collector.runMarketPricesNonEquity(['BTC-USD']);
  assert.deepEqual(fills, [{ category: 'crypto', ticker: 'BTC-USD', from: iso(-10), to: iso(0) }]);
  assert.equal(msgs.filter(m => m === HEAL_MSG(10)).length, 1);
  assert.ok(msgs.some(m => m.includes('0/1 skipped')));
});

test('coverage 15 days behind + heal 10 -> ONE merged range from gap start to today', async () => {
  store.getGaps = async () => [{ from: iso(-14), to: iso(0) }];
  await collector.runMarketPricesNonEquity(['BTC-USD']);
  assert.deepEqual(fills, [{ category: 'crypto', ticker: 'BTC-USD', from: iso(-14), to: iso(0) }]);
});

test('heal days 0 -> range gaps only; covered crypto skipped; no notice', async () => {
  process.env.OPENCLAW_CRYPTO_BARS_HEAL_DAYS = '0';
  await collector.runMarketPricesNonEquity(['BTC-USD']);
  assert.equal(fills.length, 0);
  assert.ok(msgs.some(m => m.includes('1/1 skipped')));
  assert.ok(!msgs.some(m => m.includes('self-heal')));
});

test('covered ETF is skipped with no fetch and no heal notice', async () => {
  await collector.runMarketPricesNonEquity(['SPY']);
  assert.equal(fills.length, 0);
  assert.ok(msgs.some(m => m.includes('1/1 skipped')));
  assert.ok(!msgs.some(m => m.includes('self-heal')));
});

test('_mergeGaps merges overlapping and adjacent ranges, keeps disjoint apart, sorted', () => {
  assert.deepEqual(
    collector._mergeGaps([{ from: '2026-11-01', to: '2026-11-05' }], [{ from: '2026-11-06', to: '2026-11-08' }]),
    [{ from: '2026-11-01', to: '2026-11-08' }]);
  assert.deepEqual(
    collector._mergeGaps([{ from: '2026-11-10', to: '2026-11-12' }], [{ from: '2026-11-01', to: '2026-11-03' }, { from: '2026-11-02', to: '2026-11-04' }]),
    [{ from: '2026-11-01', to: '2026-11-04' }, { from: '2026-11-10', to: '2026-11-12' }]);
  assert.deepEqual(collector._mergeGaps([], []), []);
});

test('cryptoHealDays parsing', () => {
  assert.equal(collector.cryptoHealDays({}), 10);
  assert.equal(collector.cryptoHealDays({ OPENCLAW_CRYPTO_BARS_HEAL_DAYS: '0' }), 0);
  assert.equal(collector.cryptoHealDays({ OPENCLAW_CRYPTO_BARS_HEAL_DAYS: 'abc' }), 10);
  assert.equal(collector.cryptoHealDays({ OPENCLAW_CRYPTO_BARS_HEAL_DAYS: '-3' }), 10);
  assert.equal(collector.cryptoHealDays({ OPENCLAW_CRYPTO_BARS_HEAL_DAYS: '5' }), 5);
});
