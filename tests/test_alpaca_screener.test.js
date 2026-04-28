'use strict';

/**
 * tests/test_alpaca_screener.test.js
 *
 * Unit tests for src/pipeline/alpaca_screener.js (Phase 2.2 of alpaca-cli
 * integration). Uses fake-CLI stubs so we exercise the screener-fetch logic
 * without live Alpaca calls. The DB write path is tested via dry-run to
 * avoid needing a live Postgres in unit tests.
 *
 * Run:
 *   node --test tests/test_alpaca_screener.test.js
 */

const { test } = require('node:test');
const assert    = require('node:assert/strict');
const fs        = require('node:fs');
const os        = require('node:os');
const path      = require('node:path');

function makeFakeCli(scriptBody) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-scr-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(bin, `#!/bin/bash\n${scriptBody}\n`);
  fs.chmodSync(bin, 0o755);
  return bin;
}

function loadModule(cliPath) {
  process.env.ALPACA_CLI_BIN = cliPath;
  delete require.cache[require.resolve('../src/channels/api/alpaca_cli')];
  delete require.cache[require.resolve('../src/pipeline/alpaca_screener')];
  return require('../src/pipeline/alpaca_screener');
}

test('fetchScreenerSymbols dedupes movers + most-actives', async () => {
  // CLI returns different bodies per subcommand. Branch on argv:
  //   movers     → 3 gainers + 2 losers (5 unique symbols)
  //   most-actives → 4 actives (1 overlapping with gainers, 3 new)
  // Total expected unique: 5 + 3 = 8
  const bin = makeFakeCli(`
case "$3" in
  movers)
    printf '%s' '{"gainers":[{"symbol":"AAPL"},{"symbol":"MSFT"},{"symbol":"NVDA"}],"losers":[{"symbol":"TSLA"},{"symbol":"GOOG"}]}'
    ;;
  most-actives)
    printf '%s' '{"most_actives":[{"symbol":"NVDA"},{"symbol":"SPY"},{"symbol":"QQQ"},{"symbol":"AMZN"}]}'
    ;;
esac
`);
  const { fetchScreenerSymbols } = loadModule(bin);
  const symbols = await fetchScreenerSymbols({ topN: 50 });
  assert.equal(symbols.length, 8);
  assert.ok(symbols.includes('NVDA'));   // present in both
  assert.ok(symbols.includes('AAPL'));
  assert.ok(symbols.includes('SPY'));
});

test('ingestScreenerCandidates dry-run reports counts without DB writes', async () => {
  const bin = makeFakeCli(`
case "$3" in
  movers)
    printf '%s' '{"gainers":[{"symbol":"AAPL"}],"losers":[{"symbol":"TSLA"}]}'
    ;;
  most-actives)
    printf '%s' '{"most_actives":[{"symbol":"NVDA"}]}'
    ;;
esac
`);
  const { ingestScreenerCandidates } = loadModule(bin);
  const r = await ingestScreenerCandidates({ topN: 10, dryRun: true });
  assert.equal(r.dryRun,     true);
  assert.equal(r.discovered, 3);
  assert.equal(r.inserted,   0);
  assert.deepEqual(r.symbols.sort(), ['AAPL', 'NVDA', 'TSLA']);
});

test('CLI failure on movers throws clear error', async () => {
  const bin = makeFakeCli(`
case "$3" in
  movers)
    printf '%s' '{"status":403,"error":"screener not enabled for free accounts"}' >&2
    exit 1
    ;;
  most-actives)
    printf '%s' '{"most_actives":[]}'
    ;;
esac
`);
  const { fetchScreenerSymbols } = loadModule(bin);
  await assert.rejects(
    () => fetchScreenerSymbols({ topN: 10 }),
    /screener movers failed/,
  );
});
