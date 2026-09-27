'use strict';

/**
 * tests/scripts/test_refill_price_gap.test.js
 *
 * Unit tests for scripts/refill_price_gap.js. No real Alpaca calls, no real
 * parquet reads/writes — store/collector/readPanel are always stubbed via
 * run()'s deps parameter. See task-1-brief.md / task-1-report.md.
 *
 * Run (niced, single file, per the 2-core/8GB box convention):
 *   nice -n 19 node --test tests/scripts/test_refill_price_gap.test.js
 */

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const mod = require(path.join(path.resolve(__dirname, '..', '..'), 'scripts', 'refill_price_gap.js'));
const { isEquityTicker, parseArgs, deadlineTs, selectMissing, panelToByDate, run } = mod;

// ── isEquityTicker ───────────────────────────────────────────────────────────

test('isEquityTicker matches lib/price_panel.is_equity_ticker semantics', () => {
  assert.equal(isEquityTicker('AAPL'), true);
  assert.equal(isEquityTicker('BRK-B'), true); // dash share-class, not "-USD"
  assert.equal(isEquityTicker('SPY'), true);
  assert.equal(isEquityTicker('^VIX'), false);
  assert.equal(isEquityTicker('^GSPC'), false);
  assert.equal(isEquityTicker('BTC-USD'), false);
  assert.equal(isEquityTicker('ETH-USD'), false);
  assert.equal(isEquityTicker('ES=F'), false);
  assert.equal(isEquityTicker('USDCNH=X'), false);
});

// ── parseArgs ────────────────────────────────────────────────────────────────

test('parseArgs: defaults to dry-run with required from/to', () => {
  const o = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16']);
  assert.equal(o.from, '2026-09-15');
  assert.equal(o.to, '2026-09-16');
  assert.equal(o.dryRun, true);
  assert.equal(o.apply, false);
  assert.equal(o.limit, 0);
  assert.equal(o.deadline, '19:30');
  assert.equal(o.flushEvery, 400);
});

test('parseArgs: --apply flips dryRun off', () => {
  const o = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--apply']);
  assert.equal(o.apply, true);
  assert.equal(o.dryRun, false);
});

test('parseArgs: --apply and --dry-run together is rejected', () => {
  assert.throws(
    () => parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--apply', '--dry-run']),
    /mutually exclusive/
  );
});

test('parseArgs: requires both --from and --to', () => {
  assert.throws(() => parseArgs(['--to', '2026-09-16']), /--from and --to are required/);
  assert.throws(() => parseArgs(['--from', '2026-09-15']), /--from and --to are required/);
  assert.throws(() => parseArgs([]), /--from and --to are required/);
});

test('parseArgs: rejects malformed dates and to < from', () => {
  assert.throws(() => parseArgs(['--from', '09-15-2026', '--to', '2026-09-16']), /YYYY-MM-DD/);
  assert.throws(() => parseArgs(['--from', '2026-09-15', '--to', 'not-a-date']), /YYYY-MM-DD/);
  assert.throws(() => parseArgs(['--from', '2026-09-16', '--to', '2026-09-15']), /--to must be >= --from/);
});

test('parseArgs: --limit / --deadline / --flush-every parse and validate', () => {
  const o = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--limit', '50', '--deadline', '18:00', '--flush-every', '250']);
  assert.equal(o.limit, 50);
  assert.equal(o.deadline, '18:00');
  assert.equal(o.flushEvery, 250);
  assert.throws(() => parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--flush-every', '0']), /--flush-every must be a positive integer/);
  assert.throws(() => parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--limit', '-1']), /--limit must be a non-negative integer/);
});

// ── deadlineTs ───────────────────────────────────────────────────────────────

test('deadlineTs: resolves to TODAY at HH:MM UTC and does NOT roll to tomorrow when already past', () => {
  const now = Date.UTC(2026, 8, 26, 20, 0, 0); // 2026-09-26 20:00 UTC
  const ts = deadlineTs('19:30', now);
  const d = new Date(ts);
  assert.equal(d.getUTCFullYear(), 2026);
  assert.equal(d.getUTCMonth(), 8);
  assert.equal(d.getUTCDate(), 26); // same day, not rolled to the 27th
  assert.equal(d.getUTCHours(), 19);
  assert.equal(d.getUTCMinutes(), 30);
  assert.ok(ts < now, 'deadline computed as already in the past relative to now');
});

test('deadlineTs: rejects a malformed deadline string', () => {
  assert.throws(() => deadlineTs('7pm'), /--deadline must be HH:MM/);
});

// ── selectMissing: focused single-date-of-two case ──────────────────────────

test('selectMissing: a ticker missing one of two sessions gets an item for only that date (never the blanket range)', () => {
  const byDate = new Map([
    ['2026-09-14', new Set(['AAPL'])], // anchor before
    ['2026-09-15', new Set(['AAPL'])], // present
    ['2026-09-16', new Set()],         // missing
    ['2026-09-17', new Set(['AAPL'])], // anchor after
  ]);
  const sel = selectMissing({
    byDate,
    sessions: ['2026-09-15', '2026-09-16'],
    anchorBefore: '2026-09-14',
    anchorAfter: '2026-09-17',
  });
  assert.deepEqual(sel.missingTickers, ['AAPL']);
  assert.deepEqual(sel.items, [{ ticker: 'AAPL', from: '2026-09-16', to: '2026-09-16' }]);
});

// ── selectMissing: synthetic 6-session panel, 80% hole, per the brief ───────

const SESSIONS = ['2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18', '2026-09-19', '2026-09-22'];
const ANCHOR_BEFORE = '2026-09-14';
const ANCHOR_AFTER = '2026-09-23';
const ALL_DATES = [ANCHOR_BEFORE, ...SESSIONS, ANCHOR_AFTER];

function buildByDate(presence) {
  const byDate = new Map();
  for (const d of ALL_DATES) byDate.set(d, new Set());
  for (const [ticker, dates] of Object.entries(presence)) {
    for (const d of dates) byDate.get(d).add(ticker);
  }
  return byDate;
}

function buildFixture() {
  const presence = {};
  const holeTickers = [];
  const holeDates = new Set(['2026-09-17', '2026-09-18']);
  for (let i = 1; i <= 16; i++) {
    const t = `HOLE${String(i).padStart(2, '0')}`;
    holeTickers.push(t);
    presence[t] = ALL_DATES.filter((d) => !holeDates.has(d));
  }
  const fullTickers = [];
  for (let i = 1; i <= 4; i++) {
    const t = `FULL${i}`;
    fullTickers.push(t);
    presence[t] = [...ALL_DATES];
  }
  presence['^VIX'] = ALL_DATES.filter((d) => !holeDates.has(d));          // non-equity, has a hole
  presence['CRYPTOX-USD'] = ALL_DATES.filter((d) => !holeDates.has(d));   // non-equity, has a hole
  presence['NEWCO'] = [ANCHOR_AFTER];                                     // only anchor-after (new listing)
  presence['DELISTCO'] = [ANCHOR_BEFORE];                                 // only anchor-before (delisted)
  presence['SPOTTY'] = ALL_DATES.filter((d) => d !== '2026-09-15' && d !== '2026-09-19'); // 2 separate single-day holes
  return { byDate: buildByDate(presence), holeTickers, fullTickers };
}

test('selectMissing: 80%-hole synthetic panel — 16/20 equity tickers selected, 4 clean, non-equity excluded', () => {
  const { byDate, holeTickers, fullTickers } = buildFixture();
  const sel = selectMissing({ byDate, sessions: SESSIONS, anchorBefore: ANCHOR_BEFORE, anchorAfter: ANCHOR_AFTER });

  for (const t of holeTickers) assert.ok(sel.missingTickers.includes(t), `${t} should be missing`);
  for (const t of fullTickers) assert.ok(!sel.missingTickers.includes(t), `${t} should NOT be missing`);
  assert.ok(!sel.missingTickers.includes('^VIX'), 'non-equity index ticker must be excluded despite its hole');
  assert.ok(!sel.missingTickers.includes('CRYPTOX-USD'), 'non-equity crypto ticker must be excluded despite its hole');

  // One-sided-anchor tickers are still caught (OR rule) with a run spanning
  // every session (they were never present in the window at all).
  assert.ok(sel.missingTickers.includes('NEWCO'));
  assert.ok(sel.missingTickers.includes('DELISTCO'));
  assert.deepEqual(
    sel.items.filter((it) => it.ticker === 'NEWCO'),
    [{ ticker: 'NEWCO', from: '2026-09-15', to: '2026-09-22' }]
  );
  assert.deepEqual(
    sel.items.filter((it) => it.ticker === 'DELISTCO'),
    [{ ticker: 'DELISTCO', from: '2026-09-15', to: '2026-09-22' }]
  );

  // Non-contiguous double hole -> two separate single-day items, not one span.
  assert.deepEqual(
    sel.items.filter((it) => it.ticker === 'SPOTTY').sort((a, b) => a.from.localeCompare(b.from)),
    [
      { ticker: 'SPOTTY', from: '2026-09-15', to: '2026-09-15' },
      { ticker: 'SPOTTY', from: '2026-09-19', to: '2026-09-19' },
    ]
  );

  // Each HOLE ticker gets exactly one item covering exactly the 2-date hole.
  for (const t of holeTickers) {
    assert.deepEqual(
      sel.items.filter((it) => it.ticker === t),
      [{ ticker: t, from: '2026-09-17', to: '2026-09-18' }]
    );
  }

  assert.equal(sel.missingTickers.length, 19); // 16 HOLE + NEWCO + DELISTCO + SPOTTY
  assert.equal(sel.items.length, 20);          // 16 + 1 + 1 + 2(SPOTTY)

  // OR-anchor vs AND-anchor diagnostic (advisor-requested breakdown).
  assert.equal(sel.anchorTickerCount, 23);     // 16+4+VIX+CRYPTO+NEWCO+DELISTCO+SPOTTY minus 2 non-equity = 23
  assert.equal(sel.anchorTickerCountAnd, 21);  // present BOTH anchors, equity: 16 HOLE + 4 FULL + SPOTTY
  assert.equal(sel.missingCountAnd, 17);       // of those 21: 16 HOLE + SPOTTY have a hole

  // Per-date "before" counts dip exactly on the hole dates, mirroring the
  // real 09-15/09-16 incident's shape (full universe most days, ~5 on the
  // hole days here because only FULL(4)+SPOTTY(1) are present).
  assert.deepEqual(sel.beforeCounts, {
    '2026-09-15': 22,
    '2026-09-16': 23,
    '2026-09-17': 5,
    '2026-09-18': 5,
    '2026-09-19': 22,
    '2026-09-22': 23,
  });
});

// ── run(): dry-run performs zero writes / zero Alpaca calls ─────────────────

// Fixed clock for tests that don't exercise deadline logic themselves —
// comfortably before the default 19:30 UTC deadline, independent of the
// real wall-clock time the suite happens to run at.
const FIXED_MORNING = Date.UTC(2026, 8, 26, 10, 0, 0); // 2026-09-26 10:00 UTC

function throwingSpy(name) {
  return new Proxy(
    {},
    {
      get() {
        return () => {
          throw new Error(`${name} should never be called in this scenario`);
        };
      },
    }
  );
}

function makeSyntheticPanel() {
  return {
    sessions: ['2026-09-15', '2026-09-16'],
    anchor_before: '2026-09-14',
    anchor_after: '2026-09-17',
    rows: [
      ['AAPL', '2026-09-14'], ['AAPL', '2026-09-15'], ['AAPL', '2026-09-16'], ['AAPL', '2026-09-17'], // AAPL fully present
      ['MSFT', '2026-09-14'], ['MSFT', '2026-09-17'],                                                 // MSFT missing both sessions
    ],
    total_rows: 19_381_636,
    peak_rss_kb: 135_808,
  };
}

test('run(): dry-run makes zero writes and zero Alpaca calls by construction', async () => {
  const opts = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16']); // no --apply
  const panel = makeSyntheticPanel();
  const result = await run(opts, {
    skipPreflight: true,
    now: () => FIXED_MORNING,
    readPanel: () => panel,
    store: throwingSpy('store'),
    collector: throwingSpy('collector'),
  });
  assert.equal(result.dryRun, true);
  assert.deepEqual(result.sel.missingTickers, ['MSFT']);
  assert.deepEqual(result.items, [{ ticker: 'MSFT', from: '2026-09-15', to: '2026-09-16' }]);
});

// ── run(): deadline guard aborts BEFORE any fetch/flush ─────────────────────

test('run(): refuses to start at all once the deadline has already passed today (no rollover)', async () => {
  const opts = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--apply', '--deadline', '19:30']);
  const fixedNow = deadlineTs('19:30', Date.now()) + 60_000; // 1 min after today's deadline
  await assert.rejects(
    () =>
      run(opts, {
        skipPreflight: true,
        now: () => fixedNow,
        readPanel: () => {
          throw new Error('readPanel should not be reached once the deadline has passed');
        },
        store: throwingSpy('store'),
        collector: throwingSpy('collector'),
      }),
    /deadline .* already passed/
  );
});

test('run(): deadline guard aborts before the first flush when the write-margin has already elapsed', async () => {
  const opts = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--apply']);
  const panel = makeSyntheticPanel();
  let flushCalls = 0;
  let batchCalls = 0;

  // now() sits comfortably before the plain deadline, but within the
  // write-margin of it -> FETCH_CUTOFF has already elapsed.
  const deadlineAt = deadlineTs(opts.deadline, Date.now());
  const writeMarginMs = 5 * 60_000;
  const fixedNow = deadlineAt - 60_000; // 1 min before deadline, inside a 5-min margin

  const result = await run(opts, {
    skipPreflight: true,
    now: () => fixedNow,
    writeMarginMs,
    readPanel: () => panel,
    collector: {
      fillPricesAlpacaBatch: async () => {
        batchCalls++;
        return { calls: 1 };
      },
    },
    store: {
      flushPrices: async () => {
        flushCalls++;
        return { flushed: 1, total_after: panel.total_rows + 1 };
      },
    },
  });

  assert.equal(batchCalls, 0, 'fillPricesAlpacaBatch must not be called once inside the write-margin');
  assert.equal(flushCalls, 0, 'flushPrices must not be called once inside the write-margin');
  assert.equal(result.stoppedOnDeadline, true);
  assert.equal(result.fetched, 0);
});

// ── run(): flush slicing follows --flush-every ──────────────────────────────

test('run(): apply path slices items by --flush-every and flushes once per slice', async () => {
  const opts = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--apply', '--flush-every', '2']);
  // 5 missing tickers -> slices of [2, 2, 1]
  const panel = {
    sessions: ['2026-09-15', '2026-09-16'],
    anchor_before: '2026-09-14',
    anchor_after: '2026-09-17',
    rows: [
      ['T1', '2026-09-14'], ['T1', '2026-09-17'],
      ['T2', '2026-09-14'], ['T2', '2026-09-17'],
      ['T3', '2026-09-14'], ['T3', '2026-09-17'],
      ['T4', '2026-09-14'], ['T4', '2026-09-17'],
      ['T5', '2026-09-14'], ['T5', '2026-09-17'],
    ],
    total_rows: 1000,
    peak_rss_kb: 1000,
  };

  const sliceSizes = [];
  let total = panel.total_rows;
  const flushedSlices = [];

  const afterPanel = { ...panel, rows: [] }; // pretend everything now has rows (no longer missing)

  const result = await run(opts, {
    skipPreflight: true,
    now: () => FIXED_MORNING,
    readPanel: (() => {
      let call = 0;
      return () => {
        call++;
        return call === 1 ? panel : afterPanel;
      };
    })(),
    collector: {
      fillPricesAlpacaBatch: async (items, { onTicker }) => {
        sliceSizes.push(items.length);
        for (const it of items) await onTicker(it.ticker, 1, null);
        return { calls: 1 };
      },
    },
    store: {
      flushPrices: async () => {
        const flushed = sliceSizes[sliceSizes.length - 1];
        total += flushed;
        flushedSlices.push(flushed);
        return { flushed, total_after: total };
      },
    },
  });

  assert.deepEqual(sliceSizes, [2, 2, 1]);
  assert.deepEqual(flushedSlices, [2, 2, 1]);
  assert.equal(result.fetched, 5);
  assert.equal(result.rowsWritten, 5);
  assert.equal(result.errors, 0);
});

// ── run(): flush invariant catches a mismatched total_after ─────────────────

test('run(): aborts loudly when flushPrices() total_after does not match prev + flushed', async () => {
  const opts = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--apply']);
  const panel = makeSyntheticPanel();
  await assert.rejects(
    () =>
      run(opts, {
        skipPreflight: true,
        now: () => FIXED_MORNING,
        readPanel: () => panel,
        collector: {
          fillPricesAlpacaBatch: async (items, { onTicker }) => {
            for (const it of items) await onTicker(it.ticker, 1, null);
            return { calls: 1 };
          },
        },
        store: {
          // Wrong on purpose: total_after doesn't reflect prev+flushed
          // (simulates a concurrent writer, or OPENCLAW_DRY_RUN=1 leaking in).
          flushPrices: async () => ({ flushed: 2, total_after: panel.total_rows }),
        },
      }),
    /FLUSH INVARIANT VIOLATED/
  );
});

// ── panelToByDate ────────────────────────────────────────────────────────────

test('panelToByDate: converts [ticker,date] row pairs into a date->Set(ticker) map', () => {
  const byDate = panelToByDate([
    ['AAPL', '2026-09-15'],
    ['MSFT', '2026-09-15'],
    ['AAPL', '2026-09-16'],
  ]);
  assert.deepEqual([...byDate.get('2026-09-15')].sort(), ['AAPL', 'MSFT']);
  assert.deepEqual([...byDate.get('2026-09-16')].sort(), ['AAPL']);
});
