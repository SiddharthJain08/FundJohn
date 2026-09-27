#!/usr/bin/env node
'use strict';

/**
 * refill_price_gap.js — targeted append-only refill for a hole in
 * data/master/prices.parquet.
 *
 * WHY THIS EXISTS (2026-09-26): a 2026-09-15 johnbot restart SIGKILLed the
 * EOD collect mid-write, and a 2026-09-16 global OOM did it again. Result:
 * 1,490 / 1,491 rows on those two sessions vs ~6,500 on neighbouring days —
 * every lookback crossing 09-15/09-16 is reading a partial panel (SPY, AAPL,
 * MSFT, NVDA, GOOGL among the missing). This script closes exactly that kind
 * of hole for an arbitrary --from/--to window.
 *
 * DESIGN (mirrors scripts/backfill_price_history.js's proven path):
 *   fillPricesAlpacaBatch -> store.upsertPrices (buffer) -> store.flushPrices()
 *   -> parquet_store.append_dedup (idempotent UPSERT on (ticker,date)).
 *
 * CORE INVARIANT — APPEND-ONLY: this script only ADDS rows. It never deletes,
 * truncates, or rewrites a row the ticker already has correctly. That means
 * it does NOT do a blanket fillPricesAlpaca(ticker, from, to) for every
 * candidate ticker — append_dedup's mode='replace' means an incoming batch
 * row WINS on a (ticker,date) key conflict, so re-fetching a date the ticker
 * already holds would silently overwrite a perfectly good existing row with
 * a (possibly-stale-quote) re-fetch. Instead, selectMissing() computes each
 * ticker's exact CONTIGUOUS run of missing sessions and only that range is
 * requested.
 *
 * TICKER SET: equity tickers (lib/price_panel.is_equity_ticker semantics —
 * ported inline below, see isEquityTicker) that have a row on the trading
 * session immediately before --from OR immediately after --to (i.e. were
 * actively trading around the hole) but are missing a row on at least one
 * session inside [--from, --to]. Session dates and the before/after anchor
 * dates are read from data/master/trading_calendar.parquet (active=true)
 * rather than assumed from calendar-day arithmetic, so this is correct
 * across weekends/holidays and even if [--from,--to] itself contains a
 * session where EVERY ticker's row is missing (a total-blackout day, exactly
 * this incident's shape) — trading_calendar still says it was a session.
 *
 * BOUNDED READ: the ticker x date panel needed to compute the above is read
 * via a python3 subprocess using pyarrow.dataset with column projection
 * (['ticker','date'] only) and a date-`isin` filter, which pyarrow/parquet
 * prunes by row-group min/max stats — never `pd.read_parquet` of the whole
 * 640MB/19.4M-row file. Measured on the live master for the 09-15/09-16 hole:
 * ~136MB peak RSS, <1s wall, ~16k rows fetched (see task-1-report.md).
 *
 * SAFETY
 *  - HARD DEADLINE, does not roll to tomorrow. scripts/backfill_price_history.js's
 *    deadlineTs() rolls the deadline to the next day once `now` is past it —
 *    fine for an operator-launched one-off, wrong here: a run started after
 *    19:30 UTC must refuse to start, not run straight through the 20:15Z
 *    collect / 21:30Z fleet start tonight.
 *  - The writer and the daily collector both go through
 *    parquet_store.append_dedup, which takes an exclusive fcntl flock on a
 *    {path}.lock sidecar BEFORE its read-scan-then-atomic-replace (tmp +
 *    os.replace) of prices.parquet. Concurrent writers do NOT silently lose
 *    rows — flock serializes them, and whichever one loses the race blocks
 *    up to 30s and then THROWS (TimeoutError) if the other is still holding
 *    the lock (src/data/parquet_store.py `_file_lock`). That failure mode is
 *    exactly as bad for us in practice: a daily collect that hits this
 *    script's lock and times out is a failed production cycle, not a merge.
 *    The deadline exists to keep this script's write finished — clean slot,
 *    lock released — well before the collector's own write starts, so
 *    neither process ever has to wait on the other's lock at all.
 *    (Note: this is a DIFFERENT timeout from the 30s lock timeout above —
 *    that one bounds waiting for the lock; this one bounds the writer
 *    subprocess itself once it holds it.)
 *  - Before starting any fetch, and before every flush, this refuses to
 *    proceed once `now + write-margin >= deadline` (write-margin is
 *    `--write-margin <minutes>`, default 5 — matching parquet_store.js's own
 *    write_prices op timeout, the 5-min SIGKILL on the Python writer
 *    subprocess — clamped to a floor of 1 minute) — so a flush is never
 *    *started* close enough to the deadline that it could still be running
 *    when the collector's own write begins.
 *  - Flush invariant: after every flushPrices(), this requires
 *    total_after === (running total before) + flushed, and aborts loudly
 *    otherwise. Because selectMissing() only ever requests dates a ticker
 *    is verified NOT to have, every flushed row is a brand-new key, so this
 *    should hold exactly; a mismatch means either a concurrent writer touched
 *    the master mid-run, or OPENCLAW_DRY_RUN=1 leaked into the environment
 *    (store.js's OWN dry-run gate, distinct from this script's --dry-run,
 *    which would make flushPrices() a silent no-op that still resolves).
 *  - Idempotent / resumable by construction: there is no checkpoint file.
 *    A re-run recomputes the missing set straight from the master, so a
 *    partial run (deadline hit, crash, ^C) can simply be re-run.
 *  - Preflight refuses to run against a prices.parquet that does not exist
 *    or is implausibly small (e.g. this script's own dev worktree has no
 *    data/ at all — data/* is gitignored — so accidentally running it there
 *    fails closed instead of silently writing a fresh, nearly-empty parquet
 *    under the worktree).
 *  - Never touches data_coverage directly — flushPrices()'s own
 *    _commitPriceCoverage derives it from the rows that were actually
 *    written, same as the daily collector.
 *
 * Usage:
 *   node scripts/refill_price_gap.js --from 2026-09-15 --to 2026-09-16                # dry run (default)
 *   node scripts/refill_price_gap.js --from 2026-09-15 --to 2026-09-16 --apply
 *   node scripts/refill_price_gap.js --from 2026-09-15 --to 2026-09-16 --apply --limit 50   # smoke test
 *   node scripts/refill_price_gap.js --from 2026-09-15 --to 2026-09-16 --apply --write-margin 10  # wider safety margin
 */

const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
const PRICES_PATH = path.join(ROOT, 'data', 'master', 'prices.parquet');
const CALENDAR_PATH = path.join(ROOT, 'data', 'master', 'trading_calendar.parquet');
const PYTHON_BIN = process.env.PYTHON_BIN || 'python3';

// write_prices' own SIGKILL timeout (src/data/parquet_store.js) — a full
// append_dedup rewrite of the master is bounded by this; used as this
// script's default "don't start work we can't safely finish" margin.
const DEFAULT_WRITE_MARGIN_MS = 5 * 60_000;

// ── Equity-ticker gate (lib/price_panel.is_equity_ticker, JS twin) ───────────
// True for cash-equity / ETF tickers; False for indices (^…), crypto
// (…-USD), futures (…=F) and forex (…=X). Kept identical to the Python
// definition in src/lib/price_panel.py — do not let these drift.
function isEquityTicker(ticker) {
  const t = String(ticker);
  return !t.startsWith('^') && !t.includes('-USD') && !t.includes('=F') && !t.includes('=X');
}

// ── argv parsing ──────────────────────────────────────────────────────────
function parseArgs(argv) {
  const arg = (name, def) => {
    const i = argv.indexOf(`--${name}`);
    return i >= 0 && i + 1 < argv.length ? argv[i + 1] : def;
  };
  const has = (name) => argv.includes(`--${name}`);

  const apply = has('apply');
  const dryRunFlag = has('dry-run');
  if (apply && dryRunFlag) {
    throw new Error('--apply and --dry-run are mutually exclusive (default is dry-run; pass --apply to write)');
  }

  const from = arg('from', null);
  const to = arg('to', null);
  const dateRe = /^\d{4}-\d{2}-\d{2}$/;
  if (!from || !to) throw new Error('--from and --to are required (YYYY-MM-DD, inclusive)');
  if (!dateRe.test(from) || !dateRe.test(to)) throw new Error('--from/--to must be YYYY-MM-DD');
  if (to < from) throw new Error('--to must be >= --from');

  const limit = parseInt(arg('limit', '0'), 10);
  const flushEvery = parseInt(arg('flush-every', '400'), 10);
  if (!Number.isFinite(limit) || limit < 0) throw new Error('--limit must be a non-negative integer');
  if (!Number.isFinite(flushEvery) || flushEvery <= 0) throw new Error('--flush-every must be a positive integer');

  // --write-margin <minutes>: how close to --deadline a flush may START
  // (see the SAFETY note above). Default 5 min matches parquet_store.js's
  // own write_prices op timeout — the 5-min SIGKILL on the Python writer
  // subprocess, NOT the 30s _file_lock contention timeout — clamped to a
  // floor of 1 minute rather than rejected, since 0/negative would defeat
  // the whole guard.
  const writeMarginRaw = arg('write-margin', '5');
  const writeMarginMinutes = parseInt(writeMarginRaw, 10);
  if (!Number.isFinite(writeMarginMinutes)) throw new Error('--write-margin must be a number of minutes');
  const writeMarginMs = Math.max(1, writeMarginMinutes) * 60_000;

  return {
    from,
    to,
    apply,
    dryRun: !apply, // default ON
    limit,
    deadline: String(arg('deadline', '19:30')),
    flushEvery,
    writeMarginMs,
  };
}

// TODAY at deadline HH:MM UTC. Unlike backfill_price_history.js's
// deadlineTs(), this never rolls to tomorrow when `now` is already past it —
// see the SAFETY note above.
function deadlineTs(deadlineStr, now = Date.now()) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(deadlineStr);
  if (!m) throw new Error(`--deadline must be HH:MM (UTC), got ${JSON.stringify(deadlineStr)}`);
  const h = Number(m[1]);
  const mi = Number(m[2]);
  const d = new Date(now);
  d.setUTCHours(h, mi, 0, 0);
  return d.getTime();
}

// ── Pure ticker-set selection ────────────────────────────────────────────
//
// byDate: Map<dateStr, Set<ticker>> — every session date AND both anchor
//   dates should have an entry (an empty Set is valid — it means nobody
//   traded that day, or the collect wrote nothing at all).
// sessions: ordered array of active-session date strings, ascending,
//   covering [from, to] inclusive (from trading_calendar.parquet).
// anchorBefore / anchorAfter: the active session immediately outside
//   [from, to] on each side (either may be null at the edge of history).
//
// Returns:
//   items            — [{ ticker, from, to }], one per ticker per CONTIGUOUS
//                       run of missing sessions (never a blanket [from,to]
//                       fetch — see the APPEND-ONLY note at the top of file).
//   missingTickers    — sorted list of tickers with >=1 missing session.
//   beforeCounts      — { date: count } of tickers present on each session,
//                       as found (i.e. "before" this run's writes).
//   anchorTickerCount — |tickers present on anchorBefore OR anchorAfter|
//                       (this script's selection rule).
//   anchorTickerCountAnd / missingCountAnd — the stricter "present on BOTH
//                       anchors" diagnostic some manual verification passes
//                       use; reported alongside so an operator comparing the
//                       two numbers sees why they differ (tickers with data
//                       on only one side of the hole — new listings, recent
//                       delistings) instead of being left to guess.
function selectMissing({ byDate, sessions, anchorBefore, anchorAfter }) {
  const setFor = (d) => (d && byDate.has(d) ? byDate.get(d) : new Set());
  const beforeSet = setFor(anchorBefore);
  const afterSet = setFor(anchorAfter);

  const anchorTickers = new Set();
  for (const t of beforeSet) anchorTickers.add(t);
  for (const t of afterSet) anchorTickers.add(t);

  const isPresent = (d, ticker) => byDate.has(d) && byDate.get(d).has(ticker);

  const items = [];
  const missingTickers = [];
  for (const ticker of anchorTickers) {
    if (!isEquityTicker(ticker)) continue;
    const runs = [];
    let runStart = null;
    for (let i = 0; i < sessions.length; i++) {
      const d = sessions[i];
      if (!isPresent(d, ticker)) {
        if (runStart === null) runStart = d;
      } else if (runStart !== null) {
        runs.push([runStart, sessions[i - 1]]);
        runStart = null;
      }
    }
    if (runStart !== null) runs.push([runStart, sessions[sessions.length - 1]]);
    if (runs.length) {
      missingTickers.push(ticker);
      for (const [f, t] of runs) items.push({ ticker, from: f, to: t });
    }
  }
  missingTickers.sort();

  const beforeCounts = {};
  for (const d of sessions) beforeCounts[d] = setFor(d).size;

  // Diagnostic-only: the AND-anchor / any-missing-session count some manual
  // verification passes report. Not used for selection.
  let anchorTickerCountAnd = 0;
  let missingCountAnd = 0;
  for (const t of beforeSet) {
    if (!afterSet.has(t) || !isEquityTicker(t)) continue;
    anchorTickerCountAnd++;
    if (sessions.some((d) => !isPresent(d, t))) missingCountAnd++;
  }

  return {
    items,
    missingTickers,
    beforeCounts,
    anchorTickerCount: [...anchorTickers].filter(isEquityTicker).length,
    anchorTickerCountAnd,
    missingCountAnd,
  };
}

// ── Bounded panel reader (real I/O — never exercised by tests) ──────────────
//
// Single python3 subprocess: reads the active-session window from
// trading_calendar.parquet (column+date-range projected) to get sessions +
// anchors, then reads prices.parquet with columns=['ticker','date'] and a
// date-isin filter (row-group pruned, never the whole file). Reports its own
// peak RSS (resource.getrusage) and the master's total row count (parquet
// footer metadata only — no data read) so the invariant check in run() has a
// baseline.
const PANEL_PY_SRC = `
import sys, json, resource
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from datetime import date as _date, timedelta

from_s, to_s, prices_path, cal_path = sys.argv[1:5]
from_d = _date.fromisoformat(from_s)
to_d = _date.fromisoformat(to_s)
pad_lo = from_d - timedelta(days=14)
pad_hi = to_d + timedelta(days=14)

cal = ds.dataset(cal_path, format='parquet')
ctab = cal.to_table(
    columns=['date', 'active'],
    filter=(ds.field('date') >= pad_lo) & (ds.field('date') <= pad_hi) & (ds.field('active') == True),
)
cdf = ctab.to_pandas().sort_values('date')
cdf['date_s'] = cdf['date'].astype(str)

sessions = [d for d in cdf['date_s'] if from_s <= d <= to_s]
before = cdf.loc[cdf['date_s'] < from_s, 'date_s']
after = cdf.loc[cdf['date_s'] > to_s, 'date_s']
anchor_before = before.iloc[-1] if len(before) else None
anchor_after = after.iloc[0] if len(after) else None

if not sessions:
    print(json.dumps({'error': 'no_active_sessions', 'from': from_s, 'to': to_s}))
    sys.exit(1)

all_dates = list(sessions)
if anchor_before is not None:
    all_dates.append(anchor_before)
if anchor_after is not None:
    all_dates.append(anchor_after)

pdset = ds.dataset(prices_path, format='parquet')
ptab = pdset.to_table(columns=['ticker', 'date'], filter=ds.field('date').isin(all_dates))
pdf = ptab.to_pandas()
rows = pdf.values.tolist()

total_rows = pq.read_metadata(prices_path).num_rows
peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

print(json.dumps({
    'sessions': sessions,
    'anchor_before': anchor_before,
    'anchor_after': anchor_after,
    'rows': rows,
    'total_rows': total_rows,
    'peak_rss_kb': peak_kb,
}))
`.trim();

function readTradingPanel({ from, to, pricesPath = PRICES_PATH, calendarPath = CALENDAR_PATH, root = ROOT, pythonBin = PYTHON_BIN }) {
  const { spawnSync } = require('child_process');
  const r = spawnSync(pythonBin, ['-c', PANEL_PY_SRC, from, to, pricesPath, calendarPath], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: root },
    maxBuffer: 256 * 1024 * 1024,
    encoding: 'utf8',
  });
  if (r.error) throw r.error;
  if (r.status !== 0) {
    throw new Error(`readTradingPanel: python3 exited ${r.status}: ${(r.stderr || '').slice(0, 2000)}`);
  }
  const parsed = JSON.parse(r.stdout);
  if (parsed && parsed.error) {
    throw new Error(`readTradingPanel: ${parsed.error} for [${parsed.from}, ${parsed.to}]`);
  }
  return parsed;
}

function panelToByDate(rows) {
  const byDate = new Map();
  for (const row of rows) {
    const ticker = row[0];
    const date = row[1];
    if (!byDate.has(date)) byDate.set(date, new Set());
    byDate.get(date).add(ticker);
  }
  return byDate;
}

function fmtCounts(counts) {
  return Object.entries(counts)
    .map(([d, n]) => `${d}=${n}`)
    .join(' | ');
}

// ── Append-only enforcement wrapper (F1, fix round 1) ───────────────────────
//
// store.upsertPrices only ever BUFFERS rows in memory; the actual
// (ticker,date) dedup/replace happens later, on flush, in
// append_dedup(mode='replace'). That means a bar Alpaca hands back OUTSIDE
// the [from,to] range this script computed for a ticker (an off-by-one on
// the exchange's --start/--end boundary, a neighbouring session slipping
// into the response, …) would silently overwrite a perfectly good existing
// row at flush time — exactly the class of bug the APPEND-ONLY invariant at
// the top of this file exists to prevent. The post-flush total_after
// invariant does NOT catch this: it only checks the row-COUNT delta, not
// which dates moved, so a same-count swap would sail through it clean.
//
// Rather than audit for this after the fact, this wraps the shared
// `store.upsertPrices` for the life of the apply run so any bar outside
// THAT ticker's own requested run(s) is dropped before it ever reaches the
// buffer — append-only BY CONSTRUCTION, not by later audit.
// collector.js requires the `store` module object and calls
// `store.upsertPrices(...)` off it at both call sites (fillPricesAlpaca and
// _fetchMultiBarsChunk), never a destructured reference — see collector.js
// requiring './store' near the top and both call sites using `store.` — so
// mutating the property here takes effect on every path
// collector.fillPricesAlpacaBatch can take (multi-bars and the per-ticker
// fallback alike).
//
// Date derivation mirrors store.js's `_priceRow` (`b.date || iso(b.t)`) —
// kept in sync deliberately, same spirit as isEquityTicker above. A bar with
// NO derivable date is passed through to the real upsertPrices, which has
// always rejected it itself (_priceRow returns null, not counted as
// written); it is not counted as "dropped_out_of_range" here since that
// label specifically means "had a date, and it was wrong to buffer."
function _deriveBarDate(bar) {
  if (bar && bar.date) return bar.date;
  if (bar && bar.t) {
    const d = new Date(bar.t);
    if (!Number.isNaN(d.getTime())) return d.toISOString().slice(0, 10);
  }
  return null;
}

// items: the exact [{ticker,from,to}] list this run is fetching (post
// --limit). Returns { restore(), droppedTotal(), droppedByTicker }.
function wrapAppendOnlyUpsert(store, items) {
  const original = store.upsertPrices;
  const rangesByTicker = new Map();
  for (const it of items) {
    if (!rangesByTicker.has(it.ticker)) rangesByTicker.set(it.ticker, []);
    rangesByTicker.get(it.ticker).push([it.from, it.to]);
  }
  const droppedByTicker = new Map();

  store.upsertPrices = async function guardedUpsertPrices(ticker, bars, source) {
    const ranges = rangesByTicker.get(ticker);
    if (!ranges || !ranges.length) {
      // Defensive only — every upsertPrices call on the apply path is fed by
      // this run's own `items`, so this should be unreachable. Fail closed
      // rather than trust an unrequested ticker's write.
      const n = Array.isArray(bars) ? bars.length : 0;
      if (n) droppedByTicker.set(ticker, (droppedByTicker.get(ticker) || 0) + n);
      console.warn(`[refill] append-only guard: ${ticker} called upsertPrices outside this run's item set — dropping ${n} bar(s).`);
      return 0;
    }
    const inRange = [];
    let droppedHere = 0;
    for (const b of bars || []) {
      const d = _deriveBarDate(b);
      const ok = d === null || ranges.some(([f, t]) => d >= f && d <= t);
      if (ok) inRange.push(b);
      else droppedHere++;
    }
    if (droppedHere) droppedByTicker.set(ticker, (droppedByTicker.get(ticker) || 0) + droppedHere);
    return original.call(store, ticker, inRange, source);
  };

  return {
    restore() {
      store.upsertPrices = original;
    },
    droppedTotal() {
      let n = 0;
      for (const v of droppedByTicker.values()) n += v;
      return n;
    },
    droppedByTicker,
  };
}

// ── Orchestrator ─────────────────────────────────────────────────────────
//
// deps lets tests stub every side-effecting collaborator: now(), readPanel(),
// store, collector. store/collector are only ever required for real on the
// apply path (see below) so a dry run touches neither Postgres, parquet, nor
// Alpaca by construction.
async function run(opts, deps = {}) {
  const now = deps.now || (() => Date.now());
  const pricesPath = deps.pricesPath || PRICES_PATH;
  const calendarPath = deps.calendarPath || CALENDAR_PATH;
  const root = deps.root || ROOT;
  const pythonBin = deps.pythonBin || PYTHON_BIN;
  const readPanel = deps.readPanel || ((args) => readTradingPanel({ ...args, root, pythonBin }));
  const writeMarginMs = deps.writeMarginMs ?? opts.writeMarginMs ?? DEFAULT_WRITE_MARGIN_MS;

  const STOP_AT = deadlineTs(opts.deadline, now());
  if (now() >= STOP_AT) {
    throw new Error(
      `deadline ${opts.deadline} UTC has already passed for today — refusing to start ` +
        `(a rollover to tomorrow would risk running through the collect/fleet window)`
    );
  }

  if (!deps.skipPreflight) {
    if (!fs.existsSync(pricesPath)) {
      throw new Error(`prices master not found at ${pricesPath} — refusing to run (wrong ROOT / worktree?)`);
    }
    const sz = fs.statSync(pricesPath).size;
    if (sz < 100 * 1024 * 1024) {
      throw new Error(`prices master at ${pricesPath} is only ${sz} bytes — refusing to run (looks wrong)`);
    }
  }

  console.log(`[refill] master: ${pricesPath}`);
  console.log(`[refill] window: ${opts.from} .. ${opts.to} | deadline ${opts.deadline} UTC | mode ${opts.dryRun ? 'DRY-RUN' : 'APPLY'}`);

  const panel = readPanel({ from: opts.from, to: opts.to, pricesPath, calendarPath });
  const byDate = panelToByDate(panel.rows);
  const sel = selectMissing({
    byDate,
    sessions: panel.sessions,
    anchorBefore: panel.anchor_before,
    anchorAfter: panel.anchor_after,
  });

  console.log(`[refill] sessions: ${panel.sessions.join(', ')} | anchors: ${panel.anchor_before} / ${panel.anchor_after}`);
  console.log(`[refill] before counts: ${fmtCounts(sel.beforeCounts)}`);
  console.log(
    `[refill] anchor tickers (OR rule, this run's selection): ${sel.anchorTickerCount} | ` +
      `missing (any missing session): ${sel.missingTickers.length} | fetch items: ${sel.items.length}`
  );
  console.log(
    `[refill] AND-anchor diagnostic (present both sides): ${sel.anchorTickerCountAnd} | ` +
      `missing under AND: ${sel.missingCountAnd} ` +
      `(difference from the OR numbers above = tickers with data on only one side of the hole)`
  );
  console.log(`[refill] reader: bounded pyarrow.dataset (column+date-filtered) | peak RSS ${panel.peak_rss_kb} KB | master rows (before) ${panel.total_rows}`);

  let items = sel.items;
  if (opts.limit > 0) items = items.slice(0, opts.limit);
  if (opts.limit > 0 && items.length < sel.items.length) {
    console.log(`[refill] --limit ${opts.limit} applied: ${items.length}/${sel.items.length} items this run`);
  }

  if (opts.dryRun) {
    console.log(`[refill] DRY RUN — would fetch ${items.length} item(s) covering ${sel.missingTickers.length} ticker(s). No writes performed.`);
    return { dryRun: true, sel, items, panel };
  }

  // ---- apply path: only now touch store/collector (Postgres + Alpaca) ----
  if (process.env.OPENCLAW_DRY_RUN === '1') {
    // store.js has its OWN dry-run gate (distinct from this script's --dry-run):
    // with this env var set, flushPrices() no-ops and resolves {flushed:0,
    // total_after:0, dry_run:true} instead of throwing — which would otherwise
    // look like a silent, successful, zero-row apply. Refuse outright instead
    // of relying solely on the per-flush check below to catch it.
    throw new Error(
      `OPENCLAW_DRY_RUN=1 is set in the environment — store.flushPrices() would silently ` +
        `no-op instead of writing. Unset it before running --apply.`
    );
  }
  const store = deps.store || require(path.join(ROOT, 'src/pipeline/store'));
  const collector = deps.collector || require(path.join(ROOT, 'src/pipeline/collector'));

  const FETCH_CUTOFF = STOP_AT - writeMarginMs;

  let expectedTotal = panel.total_rows;
  let fetched = 0;
  let rowsWritten = 0;
  let errors = 0;
  let totalCalls = 0;
  let stoppedOnDeadline = false;

  // F1 — append-only BY CONSTRUCTION: only bars inside a ticker's own
  // requested run(s) (this run's `items`) can ever reach store's buffer.
  // Installed for the fetch/flush loop only, always restored after (even on
  // an early throw) so a monkey-patched store.upsertPrices never outlives
  // this run.
  const appendOnlyGuard = wrapAppendOnlyUpsert(store, items);
  try {
    for (let i = 0; i < items.length; i += opts.flushEvery) {
      if (now() >= FETCH_CUTOFF) {
        console.log(
          `[refill] within the ${writeMarginMs}ms write-margin of the ${opts.deadline} UTC deadline — ` +
            `stopping before the next slice (${i}/${items.length} items done).`
        );
        stoppedOnDeadline = true;
        break;
      }
      const slice = items.slice(i, i + opts.flushEvery);

      const { calls } = await collector.fillPricesAlpacaBatch(slice, {
        onTicker: async (ticker, written, err) => {
          fetched++;
          rowsWritten += written;
          if (err) {
            errors++;
            console.warn(`[refill] ${ticker}: ${String(err.message || err).slice(0, 150)}`);
          }
        },
      });
      totalCalls += calls;

      // Re-check AFTER the fetch, BEFORE the flush: fillPricesAlpacaBatch can
      // fall back to per-ticker calls (up to `flushEvery` of them, each up to
      // ~30s) for a whole slice, so the cutoff can be crossed mid-slice even
      // though it wasn't at the top of the loop. Do not flush past it — the
      // just-fetched rows sit only in store's in-memory buffer (nothing
      // durable), so dropping them here just means a re-run re-fetches that
      // slice from Alpaca; safe, if wasteful of API calls.
      if (now() >= FETCH_CUTOFF) {
        console.warn(
          `[refill] deadline margin reached mid-slice — DROPPING ${slice.length} tickers' buffered rows ` +
            `instead of flushing, to stay clear of the ${opts.deadline} UTC deadline. Re-run to pick them up ` +
            `(idempotent — nothing was written).`
        );
        stoppedOnDeadline = true;
        break;
      }

      const res = await store.flushPrices();
      if (res === 0 || res === null || res === undefined) {
        // Nothing was buffered (e.g. every item in this slice genuinely has no
        // bars — a holiday for that ticker, or already-covered by a prior slice
        // in the same run). Nothing to check or log.
      } else if (typeof res !== 'object') {
        throw new Error(`[refill] flushPrices() returned an unexpected value (${JSON.stringify(res)}) — aborting.`);
      } else if (res.dry_run) {
        // store.js's OWN dry-run gate (OPENCLAW_DRY_RUN=1), distinct from this
        // script's --dry-run: flushPrices() no-op'd instead of writing. The
        // upfront env check above should have caught this before we got here;
        // this is defense-in-depth in case the env var changed mid-run.
        throw new Error(
          `[refill] flushPrices() returned dry_run:true (OPENCLAW_DRY_RUN=1 in the environment) — ` +
            `it did NOT write. Aborting instead of silently reporting a successful apply.`
        );
      } else {
        const flushed = res.flushed || 0;
        const expectAfter = expectedTotal + flushed;
        if (res.total_after !== expectAfter) {
          throw new Error(
            `[refill] FLUSH INVARIANT VIOLATED: expected total_after=${expectAfter} ` +
              `(prev ${expectedTotal} + flushed ${flushed}), got ${res.total_after}. ` +
              `Aborting — check for a concurrent writer (the 20:15Z collect / redeploy).`
          );
        }
        expectedTotal = res.total_after;
        if (flushed) {
          console.log(
            `[refill] flushed ${flushed} rows | master total ${expectedTotal} | ` +
              `${Math.min(i + opts.flushEvery, items.length)}/${items.length} items | ${errors} err | ` +
              `rss=${Math.round(process.memoryUsage().rss / 1048576)}MB`
          );
        }
      }
    }
  } finally {
    appendOnlyGuard.restore();
  }

  const droppedOutOfRange = appendOnlyGuard.droppedTotal();
  console.log(
    `[refill] DONE — ${fetched}/${items.length} items fetched | ${rowsWritten} rows written | ${errors} errors | ` +
      `${totalCalls} Alpaca CLI calls | dropped_out_of_range=${droppedOutOfRange}`
  );
  if (stoppedOnDeadline) {
    console.log(`[refill] stopped early on the deadline guard — re-run to pick up the remaining items (idempotent, no checkpoint needed).`);
  }
  if (droppedOutOfRange) {
    console.warn(
      `[refill] append-only guard dropped ${droppedOutOfRange} out-of-range bar(s) before buffering — ` +
        `see per-ticker counts: ${[...appendOnlyGuard.droppedByTicker.entries()].map(([t, n]) => `${t}=${n}`).join(', ')}`
    );
  }

  const after = readPanel({ from: opts.from, to: opts.to, pricesPath, calendarPath });
  const afterByDate = panelToByDate(after.rows);
  const selAfter = selectMissing({
    byDate: afterByDate,
    sessions: after.sessions,
    anchorBefore: after.anchor_before,
    anchorAfter: after.anchor_after,
  });
  console.log(`[refill] after counts: ${fmtCounts(selAfter.beforeCounts)}`);
  console.log(`[refill] still missing after this run: ${selAfter.missingTickers.length} ticker(s) (some genuinely did not trade)`);

  return { dryRun: false, sel, selAfter, fetched, rowsWritten, errors, totalCalls, stoppedOnDeadline, droppedOutOfRange };
}

// ── CLI entry ─────────────────────────────────────────────────────────────
//
// argv/deps are optional and only ever supplied by tests (mirrors run()'s
// own deps pattern) — the real entry point at the bottom of this file calls
// main() with no arguments, which is exactly today's behaviour: real argv,
// real run(), real process.exit, real dotenv load.
//
// Exit codes: 0 = clean (dry-run finished, or apply covered every selected
// item with zero per-ticker errors); 1 = bad argv or a fatal error (run()
// threw); 2 = apply completed but errors > 0 — some tickers hit the
// per-ticker fallback and still failed (F2: this used to fall through to
// exit 0, indistinguishable from a fully clean run; the "still missing"
// line in run()'s own log output is unchanged, only the exit code moved);
// 3 = apply stopped early on the deadline guard (safe/idempotent re-run).
async function main(argv = process.argv.slice(2), deps = {}) {
  const runFn = deps.run || run;
  const exit = deps.exit || ((code) => process.exit(code));
  if (!deps.skipDotenv) {
    require('dotenv').config({ path: path.join(ROOT, '.env') });
  }
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (e) {
    console.error(`[refill] argv error: ${e.message}`);
    exit(1);
    return;
  }
  try {
    const result = await runFn(opts, deps.runDeps);
    if (result && result.stoppedOnDeadline) {
      // Partial success: some items were fetched but the run stopped before
      // covering everything. Exit non-zero so a scripted runbook can't treat
      // this the same as a clean, complete pass.
      console.error(`[refill] stopped early on the deadline guard — re-run to finish (idempotent).`);
      exit(3);
      return;
    }
    if (result && !result.dryRun && result.errors > 0) {
      console.error(
        `[refill] completed with ${result.errors} per-ticker fallback error(s) — some tickers are ` +
          `still missing (see the summary above). Re-run to retry them.`
      );
      exit(2);
      return;
    }
    exit(0);
  } catch (e) {
    console.error(`[refill] FATAL: ${e.message}`);
    exit(1);
  }
}

module.exports = {
  isEquityTicker,
  parseArgs,
  deadlineTs,
  selectMissing,
  panelToByDate,
  readTradingPanel,
  run,
  main,
  PRICES_PATH,
  CALENDAR_PATH,
  DEFAULT_WRITE_MARGIN_MS,
};

if (require.main === module) {
  main();
}
