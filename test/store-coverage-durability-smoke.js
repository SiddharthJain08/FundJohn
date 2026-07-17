/**
 * Smoke test: data_coverage must never claim data the parquet doesn't hold.
 *
 * Why (found 2026-07-15): upsertPrices pushes rows into an in-memory buffer and
 * returns the BUFFERED count; updateCoverage then advanced date_to on that count
 * — before flushPrices() ever wrote the parquet. Worse, the caller passed the
 * REQUESTED range (`gap.to`) rather than the max bar actually returned. Two lies:
 *
 *   1. buffer-loss: a SIGKILL between buffer-push and flush loses the rows but
 *      KEEPS the advanced coverage -> a permanent silent parquet hole. The
 *      07-13 OOM did exactly this: AAPL's parquet is missing 07-13 while
 *      coverage claims 07-14. 131 tickers have a coverage row and ZERO rows.
 *   2. over-claim: fetch [07-14,07-15], get only a 07-14 bar, advance to 07-15.
 *      390 tickers claim newer than the parquet holds (360 by exactly one day).
 *
 * Consequence: the freshness gate reads data_coverage, so it passed on a lie —
 * measured 2026-07-15, claimed 12,157/12,699 = 95.73% PASS, TRUE 11,848 = 93.30%
 * FAIL. 309 tickers were counted fresh on data that does not exist.
 *
 * The fix cannot be atomic across two stores (parquet + Postgres) without 2PC,
 * so it CHOOSES THE SKEW DIRECTION: commit coverage only AFTER the writer
 * resolves, derived from the rows actually written. Any crash then leaves
 * parquet >= coverage — getGaps simply re-fetches (idempotent, harmless) —
 * instead of coverage >= parquet, which is a silent permanent hole.
 *
 * Run: node test/store-coverage-durability-smoke.js
 */
'use strict';

const assert = require('node:assert');
const path = require('node:path');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..');

let coverageCalls = [];      // {ticker, dataType, from, to, rows}
let writeShouldFail = false;
let written = [];            // rows handed to the parquet writer

const origLoad = Module._load;
Module._load = function (request, parent, ...rest) {
  if (request.includes('database/postgres')) {
    return {
      query: async (text, values) => {
        if (/INSERT INTO data_coverage/.test(text)) {
          coverageCalls.push({ ticker: values[0], dataType: values[1], from: values[2], to: values[3], rows: values[4] });
        }
        return { rows: [] };
      },
    };
  }
  if (request.includes('data/parquet_store')) {
    return new Proxy({}, {
      get: (_t, prop) => async (rows) => {
        if (prop === 'writePrices') {
          if (writeShouldFail) throw new Error('disk full');
          written = written.concat(rows);
          return written.length;
        }
        return 0;
      },
    });
  }
  return origLoad.call(this, request, parent, ...rest);
};

const store = require(path.join(ROOT, 'src/pipeline/store.js'));

const bar = (d, c) => ({ t: `${d}T20:00:00Z`, o: c, h: c, l: c, c, v: 1000 });

(async () => {
  // ── 1. buffering alone must NOT advance coverage ───────────────────────────
  coverageCalls = []; written = [];
  const n = await store.upsertPrices('AAPL', [bar('2026-07-14', 100), bar('2026-07-15', 101)], 'alpaca');
  assert.strictEqual(n, 2, 'upsertPrices reports rows buffered');
  assert.strictEqual(coverageCalls.length, 0,
    'BUFFERED rows must NOT advance coverage — a crash here loses them and the claim would be a permanent lie');

  // ── 2. flush commits coverage from the ACTUAL rows written ─────────────────
  const res = await store.flushPrices();
  assert.strictEqual(res.flushed, 2, 'flush wrote both rows');
  assert.strictEqual(coverageCalls.length, 1, 'coverage committed once per ticker after the writer resolved');
  const c = coverageCalls[0];
  assert.strictEqual(c.ticker, 'AAPL');
  assert.strictEqual(c.dataType, 'prices');
  assert.strictEqual(c.from, '2026-07-14', 'date_from = MIN bar date actually written');
  assert.strictEqual(c.to,   '2026-07-15', 'date_to = MAX bar date actually written');
  assert.strictEqual(c.rows, 2);

  // ── 3. the over-claim is structurally impossible now ───────────────────────
  // Fetch [07-14,07-15] but only a 07-14 bar comes back: coverage must say
  // 07-14, never the requested 07-15. This is the 360-ticker one-day lie.
  coverageCalls = []; written = [];
  await store.upsertPrices('SENEB', [bar('2026-07-14', 5)], 'alpaca');
  await store.flushPrices();
  assert.strictEqual(coverageCalls[0].to, '2026-07-14',
    'coverage reflects the bar RETURNED, never the range REQUESTED');

  // ── 4. flush FAILURE must not advance coverage, and must keep the rows ─────
  coverageCalls = []; written = [];
  await store.upsertPrices('MSFT', [bar('2026-07-15', 400)], 'alpaca');
  writeShouldFail = true;
  await assert.rejects(() => store.flushPrices(), /disk full/, 'flush error propagates');
  assert.strictEqual(coverageCalls.length, 0,
    'a FAILED write must never advance coverage — this is the whole bug');
  writeShouldFail = false;
  const retry = await store.flushPrices();
  assert.strictEqual(retry.flushed, 1, 'rows were returned to the buffer and flush on retry');
  assert.strictEqual(coverageCalls.length, 1, 'coverage advances only once the write actually succeeds');
  assert.strictEqual(coverageCalls[0].to, '2026-07-15');

  // ── 5. per-ticker min/max across a mixed flush ─────────────────────────────
  coverageCalls = []; written = [];
  await store.upsertPrices('A', [bar('2026-07-10', 1), bar('2026-07-15', 2)], 'alpaca');
  await store.upsertPrices('B', [bar('2026-07-13', 3)], 'alpaca');
  await store.flushPrices();
  const byT = Object.fromEntries(coverageCalls.map(x => [x.ticker, x]));
  assert.strictEqual(coverageCalls.length, 2, 'one coverage row per ticker');
  assert.deepStrictEqual([byT.A.from, byT.A.to], ['2026-07-10', '2026-07-15'], 'A spans its own bars');
  assert.deepStrictEqual([byT.B.from, byT.B.to], ['2026-07-13', '2026-07-13'], 'B spans its own bars');

  // ── 6. empty flush is a no-op ──────────────────────────────────────────────
  coverageCalls = [];
  await store.flushPrices();
  assert.strictEqual(coverageCalls.length, 0, 'nothing buffered → no coverage write');

  console.log('store-coverage-durability-smoke: ALL PASS');
  process.exit(0);
})().catch((e) => {
  console.error('FAIL:', e.message);
  process.exit(1);
});
