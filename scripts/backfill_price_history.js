#!/usr/bin/env node
/**
 * Out-of-band deep price-history backfill.
 *
 * WHY THIS EXISTS (2026-07-16): `scripts/backfill_universe_5y.py` (SP-2 Phase B)
 * is the sanctioned driver but is unusable at current scale. Its `_promote_chunk`
 * does `pd.read_parquet(MASTER_PRICES)` + concat + full rewrite PER (ticker,year)
 * chunk. Its own comment reads "the live file is a single 394k-row parquet" — it
 * is now 9.5M rows, 24x that. At 12,431 tickers x 5 years = 62,155 chunks that is
 * ~43 days of wall-clock and repeated OOM on a 2-core/8GB no-swap box. It also
 * predates the DuckDB fix that made the collector's writer memory-bounded.
 *
 * This driver reuses the COLLECTOR's path instead, which is already proven at
 * 12k tickers/day: fillPricesAlpaca -> store.upsertPrices (in-memory buffer) ->
 * store.flushPrices() -> parquet_store.append_dedup, which scans the master from
 * disk via DuckDB (bounded, spills) rather than loading it into pandas.
 *
 * WHAT IT REPAIRS: data_coverage claims history the parquet never received
 * (11,933 tickers claim ~2016; AAPL holds 2021+). Alpaca really does have the
 * older bars — verified — so the lie has been SUPPRESSING the backfill: getGaps
 * trusts coverage, sees "covered", and never fetches. Rather than snapping
 * date_from DOWN (which would make getGaps demand ~15.5M rows through the next
 * 16:15 collect and starve signals — the incident we just fixed), this makes the
 * existing claim TRUE by fetching the missing bars. updateCoverage uses
 * LEAST(date_from), so coverage stays at 2016 and simply becomes honest.
 *
 * SAFETY
 *  - HARD DEADLINE. The daily collect (20:15 UTC) writes the same parquet, and
 *    both do read-scan-then-atomic-replace — concurrent runs SILENTLY LOSE one
 *    side's rows. This exits well before that window and never races it.
 *  - Resumable: a checkpoint file records completed tickers; re-running skips them.
 *  - Idempotent: append_dedup upserts on (ticker,date), so a partial re-run is safe.
 *  - Never fabricates coverage: store.flushPrices() derives it from durable rows.
 *
 * Usage:
 *   node scripts/backfill_price_history.js --dry-run
 *   node scripts/backfill_price_history.js --deadline 18:00 --flush-every 400
 */
'use strict';

require('dotenv').config({ path: '/root/openclaw/.env' });
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const store = require(path.join(ROOT, 'src/pipeline/store'));
const collector = require(path.join(ROOT, 'src/pipeline/collector'));

const arg = (name, def) => {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? (process.argv[i + 1] ?? true) : def;
};
const DRY         = process.argv.includes('--dry-run');
const HISTORY_FROM = String(arg('from', '2016-01-01'));
const FLUSH_EVERY = parseInt(arg('flush-every', '400'), 10);
const DEADLINE    = String(arg('deadline', '18:00'));   // UTC HH:MM — hard stop
const SPAN_FILE   = String(arg('span', '/tmp/claude-0/-root/2f0a827a-160d-4153-9e7a-37371693db4d/scratchpad/parquet_span.json'));
const CKPT        = String(arg('checkpoint', path.join(ROOT, 'data/.backfill_price_history.done')));

function deadlineTs() {
  const [h, m] = DEADLINE.split(':').map(Number);
  const d = new Date();
  d.setUTCHours(h, m, 0, 0);
  if (d.getTime() <= Date.now()) d.setUTCDate(d.getUTCDate() + 1);
  return d.getTime();
}

const dayBefore = (iso) => {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);
};

(async () => {
  const STOP_AT = deadlineTs();
  const span = JSON.parse(fs.readFileSync(SPAN_FILE, 'utf8'));

  // Only names we actually fetch: honours the inactive flags (delisted +
  // dash/dot duplicates), so deactivated junk is never backfilled.
  const full = await store.getActiveUniverse();
  const eq = full.filter(u => u.category === 'equity').map(u => u.ticker);
  const fetchable = await collector.applyResolverEnvelope(eq, new Date().toISOString().slice(0, 10));

  const done = new Set(
    fs.existsSync(CKPT) ? fs.readFileSync(CKPT, 'utf8').split('\n').filter(Boolean) : []);

  // A ticker needs work when its earliest bar is later than the target start.
  // Names with no parquet rows at all are skipped here: they are either brand new
  // (the daily collect fills them) or deactivated — a full-history fetch for
  // thousands of them is a separate decision, not this run's job.
  const work = [];
  for (const t of fetchable) {
    if (done.has(t)) continue;
    const s = span[t];
    if (!s) continue;
    if (s[0] <= HISTORY_FROM) continue;
    const to = dayBefore(s[0]);
    if (to < HISTORY_FROM) continue;
    work.push([t, HISTORY_FROM, to]);
  }

  // --limit N: smoke-test a small slice end-to-end before committing the fleet.
  const LIMIT = parseInt(arg('limit', '0'), 10);
  if (LIMIT > 0) work.length = Math.min(work.length, LIMIT);

  console.log(`[backfill] fetchable=${fetchable.length} already-done=${done.size} TO FETCH=${work.length}`);
  console.log(`[backfill] window ${HISTORY_FROM} → (each ticker's first bar − 1d)`);
  console.log(`[backfill] flush every ${FLUSH_EVERY} tickers | hard stop ${DEADLINE} UTC (${new Date(STOP_AT).toISOString()})`);
  if (work.length) {
    console.log(`[backfill] sample: ${work.slice(0, 3).map(w => `${w[0]} ${w[1]}..${w[2]}`).join(' | ')}`);
  }
  if (DRY) {
    const est = Math.round(work.length * 0.6 / 60);
    console.log(`[backfill] DRY RUN — would fetch ${work.length} tickers, est ~${est} min of API time. No writes.`);
    process.exit(0);
  }

  let fetched = 0, rows = 0, errors = 0, sinceFlush = 0;
  const t0 = Date.now();
  const flush = async () => {
    if (!sinceFlush) return;
    const r = await store.flushPrices();       // commits TRUTHFUL coverage
    sinceFlush = 0;
    const mins = ((Date.now() - t0) / 60000).toFixed(1);
    console.log(`[backfill] flushed ${r && r.flushed ? r.flushed : 0} rows | ${fetched}/${work.length} tickers | ${rows} rows | ${errors} err | ${mins}m | rss=${Math.round(process.memoryUsage().rss / 1048576)}MB`);
  };

  for (const [ticker, from, to] of work) {
    if (Date.now() >= STOP_AT) {
      console.log(`[backfill] DEADLINE ${DEADLINE} UTC reached — stopping cleanly before the 20:15 collect.`);
      break;
    }
    try {
      const n = await collector.fillPricesAlpaca(ticker, from, to);
      rows += n; sinceFlush += n;
      fs.appendFileSync(CKPT, ticker + '\n');
    } catch (e) {
      errors++;
      console.warn(`[backfill] ${ticker} ${from}..${to}: ${e.message.slice(0, 90)}`);
    }
    fetched++;
    if (fetched % FLUSH_EVERY === 0) await flush();
  }
  await flush();
  console.log(`[backfill] DONE — ${fetched} tickers, ${rows} rows, ${errors} errors, ${((Date.now() - t0) / 60000).toFixed(1)}m`);
  process.exit(0);
})().catch((e) => { console.error('[backfill] FATAL', e); process.exit(1); });
