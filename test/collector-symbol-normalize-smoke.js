/**
 * Smoke test: fillPricesAlpaca must translate EVERY dash-form universe ticker
 * to Alpaca's dot form — not just single-letter share classes.
 *
 * Why (2026-07-15): the bridge was anchored `/^[A-Z]+-[A-Z]$/`, so only a
 * single trailing letter converted (BRK-B → BRK.B). Every multi-char class
 * suffix was sent verbatim, Alpaca answered 400 "invalid symbol", and
 * fillPricesAlpaca's invalid-symbol branch swallowed it as a warn+return 0.
 * updateCoverage then (correctly) refused to advance on a zero-row fetch, so
 * 54 LIVE preferred shares sat permanently stale in data_coverage and dragged
 * the EOD freshness gate down every single day. A silent, self-perpetuating
 * gap: the fetch "succeeded", the coverage was honest, and nothing alerted.
 *
 * Probed against the live API 2026-07-15 — dash 400s, dot returns real bars:
 *   BRK-B  AGM-PRI  ALL-PRH  ATH-PRA  AKO-A  AIIA-U  ALUB-WS  OXY-WS  AHT-PRF
 * (AGM.PRI was current through 2026-07-15 while AGM-PRI had never fetched.)
 *
 * Run: node test/collector-symbol-normalize-smoke.js
 */
'use strict';

const assert = require('node:assert');
const path = require('node:path');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..');

const alpacaCalls = [];   // every args[] handed to runAlpaca

const origLoad = Module._load;
Module._load = function (request, parent, ...rest) {
  if (request.includes('channels/api/alpaca_cli')) {
    return {
      runAlpaca: async (args) => {
        alpacaCalls.push(args);
        return { ok: true, payload: { bars: [], next_page_token: null } };
      },
    };
  }
  if (request.includes('database/postgres')) return { query: async () => ({ rows: [] }) };
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

const symbolOf = (args) => args[args.indexOf('--symbol') + 1];

(async () => {
  const cases = [
    ['AAPL',    'AAPL',     'plain symbol untouched'],
    ['BRK-B',   'BRK.B',    'single-letter share class (the only case that used to work)'],
    ['AKO-A',   'AKO.A',    'single-letter share class'],
    ['AGM-PRI', 'AGM.PRI',  'PREFERRED — 3-char suffix, silently 400d before the fix'],
    ['ALL-PRH', 'ALL.PRH',  'preferred'],
    ['AHT-PRF', 'AHT.PRF',  'preferred'],
    ['AIIA-U',  'AIIA.U',   'SPAC unit'],
    ['OXY-WS',  'OXY.WS',   'warrant'],
    ['ALUB-WS', 'ALUB.WS',  'warrant'],
  ];

  for (const [ticker, expected, why] of cases) {
    alpacaCalls.length = 0;
    await collector.fillPricesAlpaca(ticker, '2026-07-14', '2026-07-15');
    assert.ok(alpacaCalls.length > 0, `${ticker}: must call the bars API`);
    const got = symbolOf(alpacaCalls[0]);
    assert.strictEqual(got, expected, `${ticker} → ${expected} (${why}); got ${got}`);
  }

  // The rows stay keyed by the UNIVERSE ticker — only the API symbol is
  // translated. If this drifts, every downstream consumer keyed on the universe
  // convention silently loses the name.
  alpacaCalls.length = 0;
  await collector.fillPricesAlpaca('AGM-PRI', '2026-07-14', '2026-07-15');
  const args = alpacaCalls[0];
  assert.ok(args.includes('--adjustment') && args[args.indexOf('--adjustment') + 1] === 'split',
    'canonical price convention stays split-adjusted (SP-7 A3)');
  assert.ok(args.includes('--feed') && args[args.indexOf('--feed') + 1] === 'sip',
    'feed stays sip');

  console.log('collector-symbol-normalize-smoke: ALL PASS');
  process.exit(0);
})().catch((e) => {
  console.error('FAIL:', e.message);
  process.exit(1);
});
