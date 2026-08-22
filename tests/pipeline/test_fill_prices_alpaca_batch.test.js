'use strict';

/**
 * collector.fillPricesAlpacaBatch — the daily price fill batched through
 * `alpaca data multi-bars` instead of one `data bars --symbol` process per
 * ticker (~5 180 × ~65 ms ≈ 6 min/day of pure call overhead, no TLS reuse).
 *
 * Contract pinned here (from live probes of the CLI on 2026-08-22):
 *   - well-formed symbols Alpaca doesn't know are silently ABSENT from `bars`
 *     (rc=0) → that ticker gets 0 rows, no upsert (today's "null bars" case)
 *   - a MALFORMED symbol (`BRK-B`, `^GSPC`, `ES=F`, `BTC-USD`) fails the whole
 *     request: rc=1 `invalid symbol: X` → pre-filter by grammar; if one still
 *     slips through, drop the named symbol and retry the chunk once
 *   - any other chunk failure falls back to the per-ticker path for that
 *     chunk so a transient 5xx degrades to today's behaviour
 *   - `--limit 10000` is total data points → chunk size shrinks with range
 *
 * Fake CLI: a bash shim that appends its argv to a log and replays the next
 * canned payload. No live data.alpaca.markets traffic.
 *
 * Run: node --test tests/pipeline/test_fill_prices_alpaca_batch.test.js
 */

const { test } = require('node:test');
const assert    = require('node:assert/strict');
const fs        = require('node:fs');
const os        = require('node:os');
const path      = require('node:path');

function makeRecordingCli(payloads) {
  // payloads: ordered [{stdout, stderr?, exit?}]; each invocation pops the next
  // entry and appends its argv (JSON) to argv.log.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-multibars-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(path.join(dir, 'payloads.json'), JSON.stringify(payloads));
  fs.writeFileSync(path.join(dir, 'calls'), '0');
  fs.writeFileSync(bin, `#!/bin/bash
D="${dir}"
N=$(cat "$D/calls"); echo $((N+1)) > "$D/calls"
python3 - "$N" "$@" <<'PY'
import json, sys, os
d = ${JSON.stringify(dir)}
n = int(sys.argv[1]); argv = sys.argv[2:]
with open(os.path.join(d, 'argv.log'), 'a') as f: f.write(json.dumps(argv) + '\\n')
ps = json.load(open(os.path.join(d, 'payloads.json')))
p = ps[n] if n < len(ps) else {'stdout': '', 'exit': 0}
sys.stdout.write(p.get('stdout', '')); sys.stderr.write(p.get('stderr', ''))
sys.exit(int(p.get('exit', 0)))
PY
`);
  fs.chmodSync(bin, 0o755);
  return { bin, dir, argv: () => fs.existsSync(path.join(dir, 'argv.log'))
    ? fs.readFileSync(path.join(dir, 'argv.log'), 'utf8').trim().split('\n').map(JSON.parse) : [] };
}

function loadCollector(cliPath, stubs = {}) {
  process.env.ALPACA_CLI_BIN = cliPath;
  delete require.cache[require.resolve('../../src/channels/api/alpaca_cli')];
  delete require.cache[require.resolve('../../src/pipeline/store')];
  delete require.cache[require.resolve('../../src/pipeline/collector')];
  const store = require('../../src/pipeline/store');
  Object.assign(store, stubs);
  return { collector: require('../../src/pipeline/collector'), store };
}

function upsertRecorder() {
  const calls = [];
  const stub = async (ticker, bars, source) => { calls.push({ ticker, bars, source }); return bars.length; };
  return { calls, stub };
}

const bar = (t, c) => ({ t: `${t}T04:00:00Z`, o: c, h: c, l: c, c, v: 1, vw: c, n: 1 });
const multiBarsCalls = (argv) => argv.filter((a) => a[0] === 'data' && a[1] === 'multi-bars');
const singleBarsCalls = (argv) => argv.filter((a) => a[0] === 'data' && a[1] === 'bars');
const flag = (a, f) => a[a.indexOf(f) + 1];

// ── pure helpers ────────────────────────────────────────────────────────────

test('_groupGapItems groups tickers by identical (from,to) range, preserving order', () => {
  const { collector } = loadCollector('/bin/false');
  const groups = collector._groupGapItems([
    { ticker: 'AAPL', from: '2026-08-21', to: '2026-08-21' },
    { ticker: 'NEWCO', from: '2016-08-22', to: '2026-08-21' },
    { ticker: 'MSFT', from: '2026-08-21', to: '2026-08-21' },
  ]);
  assert.deepEqual([...groups.keys()], ['2026-08-21|2026-08-21', '2016-08-22|2026-08-21']);
  assert.deepEqual(groups.get('2026-08-21|2026-08-21').map((i) => i.ticker), ['AAPL', 'MSFT']);
});

test('_multiBarsChunkSize: 200 symbols for a 1-day gap, a handful for a 10-year backfill, never 0', () => {
  const { collector } = loadCollector('/bin/false');
  assert.equal(collector._multiBarsChunkSize('2026-08-21', '2026-08-21'), 200);
  assert.equal(collector._multiBarsChunkSize('2026-08-18', '2026-08-21'), 200);
  const tenYears = collector._multiBarsChunkSize('2016-08-22', '2026-08-21');
  assert.ok(tenYears >= 1 && tenYears <= 4, `10y chunk should be ~3 symbols (≤8000 bars/request), got ${tenYears}`);
  assert.equal(collector._multiBarsChunkSize('2026-08-21', '2000-01-01'), 1);
});

test('_isAlpacaStockSymbol accepts dot share classes and rejects index/futures/crypto/dash forms', () => {
  const { collector } = loadCollector('/bin/false');
  for (const ok of ['AAPL', 'BRK.B', 'AGM.PRI', 'A', 'ZZZZNOPE']) assert.equal(collector._isAlpacaStockSymbol(ok), true, ok);
  for (const bad of ['BRK-B', '^GSPC', 'ES=F', 'BTC-USD', 'EURUSD=X', '', 'aapl']) assert.equal(collector._isAlpacaStockSymbol(bad), false, bad);
});

// ── fillPricesAlpacaBatch ───────────────────────────────────────────────────

test('one chunk: multi-bars args, bars fanned out per ticker, dash→dot symbol round-trips, absent symbol → 0 rows', async () => {
  const { bin, argv } = makeRecordingCli([{ stdout: JSON.stringify({
    bars: { AAPL: [bar('2026-08-21', 230)], 'BRK.B': [bar('2026-08-21', 480), bar('2026-08-20', 479)] },
    next_page_token: null,
  }) }]);
  const { calls, stub } = upsertRecorder();
  const { collector } = loadCollector(bin, { upsertPrices: stub });
  const seen = [];
  await collector.fillPricesAlpacaBatch(
    [{ ticker: 'AAPL', from: '2026-08-20', to: '2026-08-21' },
     { ticker: 'BRK-B', from: '2026-08-20', to: '2026-08-21' },
     { ticker: 'GHOST', from: '2026-08-20', to: '2026-08-21' }],
    { onTicker: async (ticker, written, err) => seen.push({ ticker, written, err }) });

  const mb = multiBarsCalls(argv());
  assert.equal(mb.length, 1, 'exactly one multi-bars request for one chunk');
  const a = mb[0];
  assert.equal(flag(a, '--symbols'), 'AAPL,BRK.B,GHOST');
  assert.equal(flag(a, '--start'), '2026-08-20');
  assert.equal(flag(a, '--end'), '2026-08-21');
  assert.equal(flag(a, '--timeframe'), '1Day');
  assert.equal(flag(a, '--adjustment'), 'split');
  assert.equal(flag(a, '--feed'), 'sip');
  assert.equal(flag(a, '--limit'), '10000');
  assert.equal(singleBarsCalls(argv()).length, 0, 'no per-ticker fallback on the happy path');

  assert.deepEqual(calls.map((c) => [c.ticker, c.bars.length, c.source]),
    [['AAPL', 1, 'alpaca'], ['BRK-B', 2, 'alpaca']], 'rows keyed by the UNIVERSE ticker (BRK-B), not the API symbol');
  assert.deepEqual(seen.sort((x, y) => x.ticker.localeCompare(y.ticker)),
    [{ ticker: 'AAPL', written: 1, err: null }, { ticker: 'BRK-B', written: 2, err: null }, { ticker: 'GHOST', written: 0, err: null }]);
});

test('pagination: follows next_page_token and accumulates a symbol split across pages', async () => {
  const { bin, argv } = makeRecordingCli([
    { stdout: JSON.stringify({ bars: { AAPL: [bar('2026-08-19', 1)], MSFT: [bar('2026-08-19', 2)] }, next_page_token: 'tok-1' }) },
    { stdout: JSON.stringify({ bars: { MSFT: [bar('2026-08-20', 3), bar('2026-08-21', 4)] }, next_page_token: '' }) },
  ]);
  const { calls, stub } = upsertRecorder();
  const { collector } = loadCollector(bin, { upsertPrices: stub });
  const seen = {};
  await collector.fillPricesAlpacaBatch(
    [{ ticker: 'AAPL', from: '2026-08-19', to: '2026-08-21' }, { ticker: 'MSFT', from: '2026-08-19', to: '2026-08-21' }],
    { onTicker: async (t, w) => { seen[t] = w; } });
  const mb = multiBarsCalls(argv());
  assert.equal(mb.length, 2);
  assert.equal(mb[0].includes('--page-token'), false);
  assert.equal(flag(mb[1], '--page-token'), 'tok-1');
  assert.deepEqual(seen, { AAPL: 1, MSFT: 3 });
  assert.equal(calls.filter((c) => c.ticker === 'MSFT').reduce((n, c) => n + c.bars.length, 0), 3);
});

test('chunking: 450 one-day tickers → 3 multi-bars requests of 200/200/50', async () => {
  const tickers = Array.from({ length: 450 }, (_, i) => `T${i}`);
  const page = (syms) => ({ stdout: JSON.stringify({ bars: Object.fromEntries(syms.map((s) => [s, [bar('2026-08-21', 1)]])), next_page_token: null }) });
  const { bin, argv } = makeRecordingCli([page(tickers.slice(0, 200)), page(tickers.slice(200, 400)), page(tickers.slice(400))]);
  const { calls, stub } = upsertRecorder();
  const { collector } = loadCollector(bin, { upsertPrices: stub });
  let ticked = 0;
  await collector.fillPricesAlpacaBatch(tickers.map((t) => ({ ticker: t, from: '2026-08-21', to: '2026-08-21' })),
    { onTicker: async () => { ticked++; } });
  const sizes = multiBarsCalls(argv()).map((a) => flag(a, '--symbols').split(',').length);
  assert.deepEqual(sizes, [200, 200, 50]);
  assert.equal(calls.length, 450);
  assert.equal(ticked, 450, 'onTicker fires once per ticker');
});

test('different gap ranges are separate requests (no range widening across tickers)', async () => {
  const { bin, argv } = makeRecordingCli([
    { stdout: JSON.stringify({ bars: { AAPL: [bar('2026-08-21', 1)] }, next_page_token: null }) },
    { stdout: JSON.stringify({ bars: { NEWCO: [bar('2026-08-21', 1)] }, next_page_token: null }) },
  ]);
  const { stub } = upsertRecorder();
  const { collector } = loadCollector(bin, { upsertPrices: stub });
  await collector.fillPricesAlpacaBatch(
    [{ ticker: 'AAPL', from: '2026-08-21', to: '2026-08-21' }, { ticker: 'NEWCO', from: '2016-08-22', to: '2026-08-21' }],
    { onTicker: async () => {} });
  const mb = multiBarsCalls(argv());
  assert.deepEqual(mb.map((a) => [flag(a, '--symbols'), flag(a, '--start')]), [['AAPL', '2026-08-21'], ['NEWCO', '2016-08-22']]);
});

test('malformed symbols never enter a batch — they take the per-ticker path (today\'s warn/skip)', async () => {
  const { bin, argv } = makeRecordingCli([
    { stdout: JSON.stringify({ bars: { AAPL: [bar('2026-08-21', 1)] }, next_page_token: null }) },      // multi-bars for AAPL
    { stdout: '', stderr: JSON.stringify({ status: 400, error: 'invalid symbol: ^GSPC' }), exit: 1 },  // per-ticker ^GSPC → warn/skip
  ]);
  const { calls, stub } = upsertRecorder();
  const { collector } = loadCollector(bin, { upsertPrices: stub });
  const seen = {};
  await collector.fillPricesAlpacaBatch(
    [{ ticker: 'AAPL', from: '2026-08-21', to: '2026-08-21' }, { ticker: '^GSPC', from: '2026-08-21', to: '2026-08-21' }],
    { onTicker: async (t, w, e) => { seen[t] = [w, e]; } });
  assert.equal(flag(multiBarsCalls(argv())[0], '--symbols'), 'AAPL', '^GSPC must not poison the batch');
  const single = singleBarsCalls(argv());
  assert.equal(single.length, 1);
  assert.equal(flag(single[0], '--symbol'), '^GSPC');
  assert.deepEqual(seen, { AAPL: [1, null], '^GSPC': [0, null] });
  assert.equal(calls.length, 1);
});

test('"invalid symbol: X" from the API drops X and retries the chunk once; X gets 0 rows', async () => {
  const { bin, argv } = makeRecordingCli([
    { stdout: '', stderr: JSON.stringify({ code: 0, status: 400, error: 'invalid symbol: ODD.X' }), exit: 1 },
    { stdout: JSON.stringify({ bars: { AAPL: [bar('2026-08-21', 1)] }, next_page_token: null }) },
  ]);
  const { stub } = upsertRecorder();
  const { collector } = loadCollector(bin, { upsertPrices: stub });
  const seen = {};
  await collector.fillPricesAlpacaBatch(
    [{ ticker: 'AAPL', from: '2026-08-21', to: '2026-08-21' }, { ticker: 'ODD.X', from: '2026-08-21', to: '2026-08-21' }],
    { onTicker: async (t, w, e) => { seen[t] = [w, e]; } });
  const mb = multiBarsCalls(argv());
  assert.deepEqual(mb.map((a) => flag(a, '--symbols')), ['AAPL,ODD.X', 'AAPL']);
  assert.deepEqual(seen, { AAPL: [1, null], 'ODD.X': [0, null] });
  assert.equal(singleBarsCalls(argv()).length, 0);
});

test('a non-symbol chunk failure falls back to the per-ticker path for that chunk only', async () => {
  const { bin, argv } = makeRecordingCli([
    { stdout: '', stderr: JSON.stringify({ status: 502, error: 'bad gateway' }), exit: 1 },   // chunk 1 fails
    { stdout: JSON.stringify({ symbol: 'AAPL', bars: [bar('2026-08-21', 1)], next_page_token: '' }) },  // per-ticker AAPL
    { stdout: '', stderr: JSON.stringify({ status: 502, error: 'still bad' }), exit: 1 },    // per-ticker MSFT → error
  ]);
  const { stub } = upsertRecorder();
  const { collector } = loadCollector(bin, { upsertPrices: stub });
  const seen = {};
  await collector.fillPricesAlpacaBatch(
    [{ ticker: 'AAPL', from: '2026-08-21', to: '2026-08-21' }, { ticker: 'MSFT', from: '2026-08-21', to: '2026-08-21' }],
    { onTicker: async (t, w, e) => { seen[t] = [w, e ? e.message : null]; } });
  assert.equal(multiBarsCalls(argv()).length, 1);
  assert.deepEqual(singleBarsCalls(argv()).map((a) => flag(a, '--symbol')), ['AAPL', 'MSFT']);
  assert.equal(seen.AAPL[0], 1);
  assert.equal(seen.MSFT[0], 0);
  assert.match(seen.MSFT[1], /still bad/, 'per-ticker error is surfaced to the caller, never swallowed');
});

// ── runHistoricalPrices wiring ──────────────────────────────────────────────

function historicalStubs(gapsByTicker, upsert) {
  const logs = [];
  return {
    logs,
    stubs: {
      getUniverseTickers: async () => Object.keys(gapsByTicker),
      getGaps: async (ticker) => gapsByTicker[ticker] || [],
      logRun: async (...a) => { logs.push(a); },
      flushPrices: async () => ({ flushed: 0, rows: [] }),
      upsertPrices: upsert,
    },
  };
}

test('runHistoricalPrices uses multi-bars by default and logs a success run per ticker', async () => {
  const { bin, argv } = makeRecordingCli([
    { stdout: JSON.stringify({ bars: { AAPL: [bar('2026-08-21', 1)], MSFT: [bar('2026-08-21', 2)] }, next_page_token: null }) },
  ]);
  delete process.env.OPENCLAW_PRICES_MULTI_BARS;
  const { stub } = upsertRecorder();
  const gap = [{ from: '2026-08-21', to: '2026-08-21' }];
  const { logs, stubs } = historicalStubs({ AAPL: gap, MSFT: gap, DONE: [] }, stub);
  const { collector } = loadCollector(bin, stubs);
  await collector.runHistoricalPrices(5);
  assert.equal(multiBarsCalls(argv()).length, 1);
  assert.equal(singleBarsCalls(argv()).length, 0);
  assert.deepEqual(logs.map((l) => [l[0], l[2], l[3]]).sort(), [['AAPL', 'success', 1], ['MSFT', 'success', 1]]);
});

test('OPENCLAW_PRICES_MULTI_BARS=0 restores the per-ticker loop', async () => {
  const one = (sym) => ({ stdout: JSON.stringify({ symbol: sym, bars: [bar('2026-08-21', 1)], next_page_token: '' }) });
  const { bin, argv } = makeRecordingCli([one('AAPL'), one('MSFT')]);
  process.env.OPENCLAW_PRICES_MULTI_BARS = '0';
  try {
    const { stub } = upsertRecorder();
    const gap = [{ from: '2026-08-21', to: '2026-08-21' }];
    const { stubs } = historicalStubs({ AAPL: gap, MSFT: gap }, stub);
    const { collector } = loadCollector(bin, stubs);
    await collector.runHistoricalPrices(5);
    assert.equal(multiBarsCalls(argv()).length, 0);
    assert.deepEqual(singleBarsCalls(argv()).map((a) => flag(a, '--symbol')), ['AAPL', 'MSFT']);
  } finally {
    delete process.env.OPENCLAW_PRICES_MULTI_BARS;
  }
});
