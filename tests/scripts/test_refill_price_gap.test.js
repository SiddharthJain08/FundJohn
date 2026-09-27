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

// ── parseArgs: --write-margin (F3 — was a phantom flag, now parsed) ────────

test('parseArgs: --write-margin defaults to 5 minutes, parses minutes to ms, and clamps to a 1-minute floor', () => {
  const dflt = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16']);
  assert.equal(dflt.writeMarginMs, 5 * 60_000);

  const custom = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--write-margin', '10']);
  assert.equal(custom.writeMarginMs, 10 * 60_000);

  const zero = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--write-margin', '0']);
  assert.equal(zero.writeMarginMs, 1 * 60_000, 'clamped up to the 1-minute floor, not rejected');

  const negative = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--write-margin', '-5']);
  assert.equal(negative.writeMarginMs, 1 * 60_000);

  assert.throws(
    () => parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--write-margin', 'abc']),
    /--write-margin must be a number/
  );
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

test('run(): deadline crossed DURING a slow fetch (e.g. per-ticker fallback) is caught before the flush, not just before the next slice', async () => {
  const opts = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--apply', '--flush-every', '10']);
  const panel = {
    sessions: ['2026-09-15', '2026-09-16'],
    anchor_before: '2026-09-14',
    anchor_after: '2026-09-17',
    rows: [
      ['T1', '2026-09-14'], ['T1', '2026-09-17'],
      ['T2', '2026-09-14'], ['T2', '2026-09-17'],
    ],
    total_rows: 1000,
    peak_rss_kb: 1000,
  };

  const writeMarginMs = 60_000; // 1 minute, for simple arithmetic
  let currentNow = FIXED_MORNING; // well before today's 19:30 UTC deadline
  const deadlineAt = deadlineTs(opts.deadline, currentNow);
  const fetchCutoff = deadlineAt - writeMarginMs;

  let flushCalls = 0;
  const result = await run(opts, {
    skipPreflight: true,
    now: () => currentNow,
    writeMarginMs,
    readPanel: () => panel,
    collector: {
      // Simulates a slow fetch (e.g. a multi-bars chunk failure falling back
      // to per-ticker calls) that eats the remaining margin entirely: by the
      // time it returns, the cutoff has already elapsed.
      fillPricesAlpacaBatch: async (items, { onTicker }) => {
        for (const it of items) await onTicker(it.ticker, 1, null);
        currentNow = fetchCutoff + 1000;
        return { calls: items.length };
      },
    },
    store: {
      flushPrices: async () => {
        flushCalls++;
        return { flushed: 2, total_after: panel.total_rows + 2 };
      },
    },
  });

  assert.equal(flushCalls, 0, 'flushPrices must not run once the fetch itself crossed the cutoff');
  assert.equal(result.stoppedOnDeadline, true);
  assert.equal(result.fetched, 2, 'onTicker still fired for the in-flight slice; only the flush was skipped');
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

test('run(): aborts when flushPrices() reports store.js\'s own OPENCLAW_DRY_RUN=1 no-op shape', async () => {
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
          // Exact shape store.js's _flush() returns when OPENCLAW_DRY_RUN=1
          // is set in the environment: flushed=0 (falsy), so a naive
          // `if (res && res.flushed)` guard would skip this silently and the
          // apply would "succeed" having written nothing.
          flushPrices: async () => ({ flushed: 0, total_after: 0, dry_run: true, would_have_written: 2 }),
        },
      }),
    /dry_run|OPENCLAW_DRY_RUN/
  );
});

// ── run(): F1 — append-only guard drops bars outside a ticker's requested run ──

test('run(): F1 — bars outside a ticker\'s requested [from,to] are dropped before buffering, never flushed', async () => {
  const opts = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--apply']);
  const panel = makeSyntheticPanel(); // MSFT missing both sessions -> one item, from=09-15 to=09-16
  const buffered = [];

  const storeStub = {
    // The REAL store.upsertPrices signature/behaviour this wrap sits in
    // front of: buffer whatever it's handed, deriving `date` the same way
    // store.js's `_priceRow` does (`b.date || iso(b.t)`), and return the
    // count buffered.
    upsertPrices: async (ticker, bars) => {
      for (const b of bars) {
        const date = b.date || new Date(b.t).toISOString().slice(0, 10);
        buffered.push({ ticker, date });
      }
      return bars.length;
    },
    flushPrices: async () => ({ flushed: buffered.length, total_after: panel.total_rows + buffered.length }),
  };

  const result = await run(opts, {
    skipPreflight: true,
    now: () => FIXED_MORNING,
    readPanel: () => panel,
    collector: {
      // Mirrors the real collector: calls store.upsertPrices(ticker, bars, source)
      // off the shared store object. Real Alpaca bars carry `t` (an RFC3339
      // timestamp), not `date` — this uses that same shape, not the `date`
      // shortcut, so the guard's default date-derivation path is exercised.
      // Simulates Alpaca handing back the exact 09-14/09-17 neighbour-bar
      // shape the review flagged, alongside the two genuinely-requested dates.
      fillPricesAlpacaBatch: async (items, { onTicker }) => {
        for (const it of items) {
          const bars = [
            { t: '2026-09-14T04:00:00Z', c: 1 },  // before the requested run — must be dropped
            { t: `${it.from}T04:00:00Z`, c: 2 },   // in range
            { t: `${it.to}T04:00:00Z`, c: 3 },     // in range
            { t: '2026-09-17T04:00:00Z', c: 4 },   // after the requested run — must be dropped
          ];
          const written = await storeStub.upsertPrices(it.ticker, bars, 'alpaca');
          await onTicker(it.ticker, written, null);
        }
        return { calls: 1 };
      },
    },
    store: storeStub,
  });

  assert.deepEqual(
    buffered.map((b) => b.date).sort(),
    ['2026-09-15', '2026-09-16'],
    'only the two in-range bars reach the buffer — the 09-14/09-17 neighbours never do'
  );
  assert.equal(result.droppedOutOfRange, 2);
  assert.equal(result.rowsWritten, 2, 'onTicker sees the POST-filter written count, not the raw fetch count');
});

// ── run(): F4 — upfront OPENCLAW_DRY_RUN=1 refusal ──────────────────────────

test('run(): F4 — refuses to apply outright when OPENCLAW_DRY_RUN=1 leaks into the environment', async () => {
  const opts = parseArgs(['--from', '2026-09-15', '--to', '2026-09-16', '--apply']);
  const panel = makeSyntheticPanel();
  const prev = process.env.OPENCLAW_DRY_RUN;
  process.env.OPENCLAW_DRY_RUN = '1';
  try {
    await assert.rejects(
      () =>
        run(opts, {
          skipPreflight: true,
          now: () => FIXED_MORNING,
          readPanel: () => panel,
          store: throwingSpy('store'),
          collector: throwingSpy('collector'),
        }),
      /OPENCLAW_DRY_RUN=1/
    );
  } finally {
    if (prev === undefined) delete process.env.OPENCLAW_DRY_RUN;
    else process.env.OPENCLAW_DRY_RUN = prev;
  }
});

// ── main(): F4 / F2 — exit-code mapping 0/1/2/3 ─────────────────────────────

test('main(): exit 0 on a clean dry-run result', async () => {
  let exitCode;
  await mod.main(['--from', '2026-09-15', '--to', '2026-09-16'], {
    skipDotenv: true,
    run: async () => ({ dryRun: true }),
    exit: (code) => { exitCode = code; },
  });
  assert.equal(exitCode, 0);
});

test('main(): exit 0 on a clean apply result (dryRun:false, errors:0, not stopped on deadline)', async () => {
  let exitCode;
  await mod.main(['--from', '2026-09-15', '--to', '2026-09-16', '--apply'], {
    skipDotenv: true,
    run: async () => ({ dryRun: false, errors: 0, stoppedOnDeadline: false }),
    exit: (code) => { exitCode = code; },
  });
  assert.equal(exitCode, 0);
});

test('main(): exit 1 on bad argv, before run() is ever called', async () => {
  let exitCode;
  let runCalled = false;
  await mod.main(['--from', '2026-09-15'], {
    skipDotenv: true,
    run: async () => {
      runCalled = true;
      return { dryRun: true };
    },
    exit: (code) => { exitCode = code; },
  });
  assert.equal(exitCode, 1);
  assert.equal(runCalled, false);
});

test('main(): exit 1 when run() throws (fatal)', async () => {
  let exitCode;
  await mod.main(['--from', '2026-09-15', '--to', '2026-09-16'], {
    skipDotenv: true,
    run: async () => {
      throw new Error('boom');
    },
    exit: (code) => { exitCode = code; },
  });
  assert.equal(exitCode, 1);
});

test('main(): F2 — exit 2 when the apply result has per-ticker errors (was silently exit 0)', async () => {
  let exitCode;
  await mod.main(['--from', '2026-09-15', '--to', '2026-09-16', '--apply'], {
    skipDotenv: true,
    run: async () => ({ dryRun: false, errors: 3, stoppedOnDeadline: false }),
    exit: (code) => { exitCode = code; },
  });
  assert.equal(exitCode, 2);
});

test('main(): exit 3 takes priority when the apply result both stopped on the deadline AND has errors', async () => {
  let exitCode;
  await mod.main(['--from', '2026-09-15', '--to', '2026-09-16', '--apply'], {
    skipDotenv: true,
    run: async () => ({ dryRun: false, errors: 2, stoppedOnDeadline: true }),
    exit: (code) => { exitCode = code; },
  });
  assert.equal(exitCode, 3);
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
