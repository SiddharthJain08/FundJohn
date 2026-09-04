/**
 * Smoke test: the EOD freshness gate must be scoped to what `signals` CONSUMES,
 * not to everything the collector opportunistically fetches.
 *
 * Why (2026-07-15 incident): the gate ran on `priceEquityTickers` — the wide
 * SP-7 C2 resolver envelope (12,699 names) whose whole purpose is to keep
 * accruing price history for names a strategy might adopt LATER. 706 of those
 * were stale on 07-14 → 94.44% < 0.95 → collect threw → `signals` never ran →
 * the sizer self-loaded an empty carried set and emitted 0 orders with rc=0
 * `status=ok` for five days. Starvation looked like success.
 *
 * But 650 of those 706 are consumed by NO live strategy. Measured on the real
 * 07-14 cohort:
 *
 *   denominator                     n       stale   frac      verdict
 *   priceEquityTickers (envelope)   12,699    706   94.44%    FAIL  <- the halt
 *   universe_config equity           5,082    174   96.58%    pass
 *   live-strategy union              7,004     56   99.20%    pass  <- 4.2pp room
 *
 * Fetch wide (history keeps growing); gate narrow (panel health only). These
 * are different questions and conflating them halted the fund over instruments
 * nobody trades.
 *
 * Run: node test/collector-freshness-scope-smoke.js
 */
'use strict';

const assert = require('node:assert');
const path = require('node:path');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..', '..');

// ── Module stubs: no real Postgres / parquet / Redis / Discord ──────────────
const origLoad = Module._load;
Module._load = function (request, parent, ...rest) {
  if (request.includes('database/postgres')) {
    return { query: async () => ({ rows: [] }) };
  }
  if (request.includes('database/redis')) {
    return { getClient: () => ({ get: async () => null, set: async () => null }) };
  }
  if (request.includes('data/parquet_store')) {
    return new Proxy({}, { get: () => async () => ({}) });
  }
  if (request.includes('budget/enforcer')) {
    return { checkBudget: async () => ({ mode: 'GREEN' }), enforceBudget: () => ({}) };
  }
  return origLoad.call(this, request, parent, ...rest);
};

const collector = require(path.join(ROOT, 'src/pipeline/collector.js'));
const { _signalsConsumedScope, _verifyEquityFreshness } = collector;

(async () => {
  const CONFIG   = ['AAPL', 'MSFT', 'MU', 'STX', 'BRK-B'];
  // The envelope is a superset: config + thousands of names no strategy trades.
  const ENVELOPE = [...CONFIG, 'AKZOY', 'BAESY', 'AACBU', 'ABVEW', 'DEADCO'];

  // ── 1. union available → gate scope is the union, not the envelope ─────────
  let scope = await _signalsConsumedScope(CONFIG, ENVELOPE, '2026-07-15', {
    unionFn: async () => ['AAPL', 'MSFT', 'MU'],
  });
  assert.deepStrictEqual(scope, ['AAPL', 'MSFT', 'MU'],
    'gate scope must be the live-strategy union');
  assert.ok(!scope.includes('AKZOY'),
    'a dead OTC ADR no strategy trades must NEVER be in the gate denominator');

  // ── 2. union is dot-form, config is dash-form → single-letter bridge ───────
  // The resolver emits BRK.B; universe_config stores BRK-B. Without the bridge
  // the name silently drops out of the scope.
  scope = await _signalsConsumedScope(CONFIG, ENVELOPE, '2026-07-15', {
    unionFn: async () => ['BRK.B', 'AAPL'],
  });
  assert.deepStrictEqual(scope, ['AAPL', 'BRK-B'], 'dot-form union bridges to dash-form fetch keys');

  // ── 3. union names we never fetch are dropped (unsatisfiable gate) ─────────
  scope = await _signalsConsumedScope(CONFIG, ENVELOPE, '2026-07-15', {
    unionFn: async () => ['AAPL', 'NEVERFETCHED'],
  });
  assert.deepStrictEqual(scope, ['AAPL'],
    'demanding freshness for a ticker no phase fetches would be unsatisfiable');

  // ── 4. degraded paths fall back to CONFIG, never to the envelope ───────────
  // This is the load-bearing assertion: falling back to priceEquityTickers
  // would silently reinstate the exact denominator that caused the halt.
  for (const [label, unionFn] of [
    ['union null',        async () => null],
    ['union empty',       async () => []],
    ['union throws',      async () => { throw new Error('redis down'); }],
    ['union disjoint',    async () => ['NOTHING', 'MATCHES']],
  ]) {
    const s = await _signalsConsumedScope(CONFIG, ENVELOPE, '2026-07-15', { unionFn });
    assert.deepStrictEqual(s, [...CONFIG].sort(),
      `${label} → falls back to universe_config`);
    assert.ok(!s.includes('AKZOY') && !s.includes('DEADCO'),
      `${label} → MUST NOT fall back to the wide envelope`);
  }

  // ── 5. REGRESSION: the real 07-14 shape ───────────────────────────────────
  // Rebuild the incident at scale: 12,699 fetched names of which 706 are stale,
  // but only 56 of the stale are consumed by a live strategy.
  const big = (n, p) => Array.from({ length: n }, (_, i) => `${p}${i}`);
  const consumed  = big(7004, 'C');                 // live-strategy union
  const unconsumed = big(5695, 'X');                // envelope-only, nobody trades
  const envBig = [...consumed, ...unconsumed];
  const staleConsumed   = new Set(consumed.slice(0, 56));    // 56 real stragglers
  const staleUnconsumed = new Set(unconsumed.slice(0, 650)); // 650 dead names
  const isStale = (t) => staleConsumed.has(t) || staleUnconsumed.has(t);
  const freshOf = (tickers) => tickers.filter(t => !isStale(t));

  // OLD behavior — gate on the envelope → the halt.
  let threw = false;
  try {
    await _verifyEquityFreshness(envBig, '2026-07-14', {
      queryFreshFn: async (t) => freshOf(t), refetchFn: async () => {}, sleepFn: async () => {},
      maxAttempts: 1,
    });
  } catch (e) { threw = true; }
  assert.ok(threw, 'PRE-FIX: gating on the envelope aborts the cycle (the 07-14 halt)');

  // NEW behavior — gate on the consumed scope → passes with room to spare.
  const gateScope = await _signalsConsumedScope(envBig, envBig, '2026-07-14', {
    unionFn: async () => consumed,
  });
  assert.strictEqual(gateScope.length, 7004, 'scope is the consumed union');
  const res = await _verifyEquityFreshness(gateScope, '2026-07-14', {
    queryFreshFn: async (t) => freshOf(t), refetchFn: async () => {}, sleepFn: async () => {},
    maxAttempts: 1,
  });
  assert.strictEqual(res.stale.length, 56, 'the 56 real stragglers are still reported');
  const frac = (gateScope.length - res.stale.length) / gateScope.length;
  assert.ok(frac > 0.99, `POST-FIX: 07-14 passes at ${(frac * 100).toFixed(2)}% (was 94.44%)`);

  // ── 6. the gate still ABORTS when the consumed panel is genuinely stale ────
  // Narrowing must not defang the gate — that would be worse than the bug.
  threw = false;
  try {
    await _verifyEquityFreshness(consumed, '2026-07-14', {
      queryFreshFn: async (t) => t.slice(0, Math.floor(t.length * 0.5)), // 50% fresh
      refetchFn: async () => {}, sleepFn: async () => {}, maxAttempts: 1,
    });
  } catch (e) { threw = true; assert.ok(/freshness/i.test(e.message)); }
  assert.ok(threw, 'a genuinely stale CONSUMED panel must still halt signals');

  console.log('collector-freshness-scope-smoke: ALL PASS');
  process.exit(0);
})().catch((e) => {
  console.error('FAIL:', e.message);
  process.exit(1);
});
