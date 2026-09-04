/**
 * Smoke test: 16:15 ET cycle collect must capture SAME-DAY closes (fix B).
 *
 * Why: getGapSummary marked a ticker `covered` when data_coverage.date_to >=
 * YESTERDAY, so the in-cycle collect could never fetch today's close — the
 * EOD compute ran on close[T−1] every day (2026-06-02/03; see
 * /root/sp6_phaseA_conviction_gate_diagnosis_2026-06-04.md §10). Same-day
 * capture belonged exclusively to the 16:30 ET eod-refresh timer, which fires
 * AFTER the compute. Operator-directed fix 2026-06-04: collect captures the
 * closes itself at 16:15 and verifies success before signals run.
 *
 *   1. store.getGapSummary honors pricesRequiredThrough (today post-close).
 *   2. _eodFreshnessContext: post-close weekday → require today; pre-close /
 *      weekend / holiday (calendar probe false) → legacy behavior.
 *      Calendar-probe FAILURE on a weekday post-close → require (fail-loud).
 *   3. _verifyEquityFreshness: retries stragglers, passes at >= minFrac,
 *      THROWS when the panel stays substantially stale (signals must not run).
 *
 * Run: node test/collector-eod-freshness-smoke.js
 */
'use strict';

const assert = require('node:assert');
const path = require('node:path');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..', '..');

// ── Module stubs: no real Postgres / parquet / Discord ──────────────────────
const sqlCalls = []; // { text, values }
let gapRows = [];    // rows returned for the getGapSummary prices query
const origLoad = Module._load;
Module._load = function (request, parent, ...rest) {
  if (request.includes('database/postgres')) {
    return {
      query: async (text, values) => {
        sqlCalls.push({ text, values });
        if (/data_type = 'prices'/.test(text) || /'prices'/.test(text)) {
          return { rows: gapRows };
        }
        return { rows: [] };
      },
    };
  }
  if (request.includes('data/parquet_store')) {
    return new Proxy({}, { get: () => async () => ({}) });
  }
  if (request.includes('budget/enforcer')) {
    return { checkBudget: async () => ({ mode: 'GREEN' }), enforceBudget: () => ({}) };
  }
  return origLoad.call(this, request, parent, ...rest);
};

const store = require(path.join(ROOT, 'src/pipeline/store.js'));
const collector = require(path.join(ROOT, 'src/pipeline/collector.js'));

(async () => {
  // ── 1. getGapSummary: pricesRequiredThrough honored ───────────────────────
  sqlCalls.length = 0;
  gapRows = [{ ticker: 'MU', status: 'needed' }];
  await store.getGapSummary({
    priceTickers: ['MU'], optionsTickers: [], fundTickers: [],
    fromDate: '2016-06-04', toDate: '2026-06-04',
    pricesRequiredThrough: '2026-06-04',
  });
  const priceCall = sqlCalls.find(c => /data_type = 'prices'/.test(c.text));
  assert.ok(priceCall, 'getGapSummary must query prices coverage');
  assert.ok(priceCall.values.flat().includes('2026-06-04'),
    `prices coverage query must require date_to >= pricesRequiredThrough (today), got params ${JSON.stringify(priceCall.values)}`);

  // legacy default unchanged: yesterday
  sqlCalls.length = 0;
  await store.getGapSummary({
    priceTickers: ['MU'], optionsTickers: [], fundTickers: [],
    fromDate: '2016-06-04', toDate: '2026-06-04',
  });
  const legacyCall = sqlCalls.find(c => /data_type = 'prices'/.test(c.text));
  assert.ok(legacyCall.values.flat().includes('2026-06-03'),
    'without pricesRequiredThrough the rule stays date_to >= yesterday');

  // ── 2. _eodFreshnessContext ────────────────────────────────────────────────
  // 2026-06-04 is a Thursday. 20:20 UTC = 16:20 ET (post-close).
  const postClose = new Date('2026-06-04T20:20:00Z');
  const preClose  = new Date('2026-06-04T15:00:00Z'); // 11:00 ET
  const saturday  = new Date('2026-06-06T20:20:00Z');

  let ctx = await collector._eodFreshnessContext({ now: postClose, calendarProbe: async () => true });
  assert.strictEqual(ctx.requireToday, true, 'post-close weekday trading day → require today');
  assert.strictEqual(ctx.todayEt, '2026-06-04', 'ET date');

  ctx = await collector._eodFreshnessContext({ now: preClose, calendarProbe: async () => true });
  assert.strictEqual(ctx.requireToday, false, 'pre-close → legacy');

  ctx = await collector._eodFreshnessContext({ now: saturday, calendarProbe: async () => true });
  assert.strictEqual(ctx.requireToday, false, 'weekend → legacy');

  ctx = await collector._eodFreshnessContext({ now: postClose, calendarProbe: async () => false });
  assert.strictEqual(ctx.requireToday, false, 'market holiday (calendar says closed) → legacy');

  ctx = await collector._eodFreshnessContext({ now: postClose, calendarProbe: async () => null });
  assert.strictEqual(ctx.requireToday, true, 'calendar probe FAILURE post-close weekday → fail-loud (require)');

  // ── 3. _verifyEquityFreshness ──────────────────────────────────────────────
  const equity = ['AAPL', 'MSFT', 'MU', 'STX'];

  // (a) all fresh on first check → no refetch
  let refetches = [];
  let res = await collector._verifyEquityFreshness(equity, '2026-06-04', {
    queryFreshFn: async () => equity,
    refetchFn: async (t) => refetches.push(t),
    sleepFn: async () => {},
  });
  assert.strictEqual(res.stale.length, 0, 'all fresh → no stale');
  assert.strictEqual(refetches.length, 0, 'all fresh → no refetch');

  // (b) stragglers recover after one refetch
  refetches = [];
  let checks = 0;
  res = await collector._verifyEquityFreshness(equity, '2026-06-04', {
    queryFreshFn: async () => (++checks === 1 ? ['AAPL', 'MSFT'] : equity),
    refetchFn: async (t) => refetches.push(t),
    sleepFn: async () => {},
  });
  assert.deepStrictEqual(refetches[0], ['MU', 'STX'], 'stale tickers get refetched');
  assert.strictEqual(res.stale.length, 0, 'recovered after retry');

  // (c) persistently stale below minFrac → throws (signals must not run)
  let threw = false;
  try {
    await collector._verifyEquityFreshness(equity, '2026-06-04', {
      queryFreshFn: async () => ['AAPL'],          // 25% fresh forever
      refetchFn: async () => {},
      sleepFn: async () => {},
      maxAttempts: 2,
    });
  } catch (e) {
    threw = true;
    assert.ok(/freshness/i.test(e.message), `error names the freshness gate: ${e.message}`);
  }
  assert.ok(threw, 'substantially stale panel must abort the cycle');

  // (d) tiny straggler tail above minFrac → passes, reports stale list
  const many = Array.from({ length: 100 }, (_, i) => `T${i}`);
  res = await collector._verifyEquityFreshness(many, '2026-06-04', {
    queryFreshFn: async () => many.slice(0, 97),   // 97% fresh
    refetchFn: async () => {},
    sleepFn: async () => {},
    maxAttempts: 1,
  });
  assert.deepStrictEqual(res.stale, ['T97', 'T98', 'T99'], 'straggler tail reported, not fatal');

  // ── 4. dormancy carve-out (2026-09-04) ────────────────────────────────────
  // Names with a KNOWN-old last bar (SPAC units/warrants that go days without
  // a trade) leave the DENOMINATOR — they have no bar to fetch, and counting
  // them made the 95% floor unreachable (halted 6 of 8 cycles since 08-26).

  // (e) dormant names excluded → 94/94 passes where 94/100 would abort
  const wide = Array.from({ length: 100 }, (_, i) => `T${i}`);
  const covMap = new Map(wide.map(t => [t, '2026-06-03']));           // active
  for (let i = 94; i < 100; i++) covMap.set(`T${i}`, '2026-05-01');   // dormant
  res = await collector._verifyEquityFreshness(wide, '2026-06-04', {
    queryFreshFn: async () => wide.slice(0, 94),
    queryCoverageFn: async () => covMap,
    refetchFn: async () => {},
    sleepFn: async () => {},
    maxAttempts: 1,
  });
  assert.strictEqual(res.stale.length, 0, 'dormant names leave the denominator entirely');

  // (f) a real outage still trips the gate: everyone traded yesterday
  threw = false;
  try {
    await collector._verifyEquityFreshness(wide, '2026-06-04', {
      queryFreshFn: async () => [],
      queryCoverageFn: async () => new Map(wide.map(t => [t, '2026-06-03'])),
      refetchFn: async () => {},
      sleepFn: async () => {},
      maxAttempts: 1,
    });
  } catch (e) { threw = true; }
  assert.ok(threw, 'recent-active names all stale = real outage → still aborts');

  // (g) dormantDays: 0 disables the carve-out (old strict behavior)
  threw = false;
  try {
    await collector._verifyEquityFreshness(wide, '2026-06-04', {
      queryFreshFn: async () => wide.slice(0, 94),
      queryCoverageFn: async () => covMap,
      refetchFn: async () => {},
      sleepFn: async () => {},
      maxAttempts: 1,
      dormantDays: 0,
    });
  } catch (e) { threw = true; }
  assert.ok(threw, 'dormantDays=0 keeps the full denominator (94% < 95% → abort)');

  // (h) coverage lookup failure → full denominator, loud old behavior
  threw = false;
  try {
    await collector._verifyEquityFreshness(wide, '2026-06-04', {
      queryFreshFn: async () => wide.slice(0, 94),
      queryCoverageFn: async () => { throw new Error('db down'); },
      refetchFn: async () => {},
      sleepFn: async () => {},
      maxAttempts: 1,
    });
  } catch (e) { threw = true; }
  assert.ok(threw, 'dormancy lookup failure must never widen the gate');

  console.log('collector-eod-freshness-smoke: ALL PASS');
  process.exit(0);
})().catch((e) => {
  console.error('FAIL:', e.message);
  process.exit(1);
});
