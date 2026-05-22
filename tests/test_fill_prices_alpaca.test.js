'use strict';

/**
 * Unit tests for collector.fillPricesAlpaca (SP-1 Task 15).
 *
 * Stubs the alpaca CLI binary so the helper can be exercised without live
 * data.alpaca.markets traffic. Same fake-CLI pattern as
 * test_alpaca_options.test.js + test_alpaca_screener.test.js.
 *
 * Run: node --test tests/test_fill_prices_alpaca.test.js
 */

const { test } = require('node:test');
const assert    = require('node:assert/strict');
const fs        = require('node:fs');
const os        = require('node:os');
const path      = require('node:path');

function makeFakeCli(stdout, { stderr = '', exit = 0 } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-bars-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(bin,
    `#!/bin/bash\nprintf '%s' ${JSON.stringify(stdout)}\nif [ -n "${stderr.replace(/"/g, '\\"')}" ]; then printf '%s' ${JSON.stringify(stderr)} 1>&2; fi\nexit ${exit}\n`);
  fs.chmodSync(bin, 0o755);
  return bin;
}

function makeMultiCli(payloads) {
  // payloads: ordered list of { stdout, stderr?, exit? } — each invocation
  // pops the next entry. Lets us cover pagination + retry flows.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-bars-'));
  const bin   = path.join(dir, 'alpaca');
  const stateFile = path.join(dir, '.calls');
  fs.writeFileSync(stateFile, '0');
  const json = JSON.stringify(payloads);
  fs.writeFileSync(bin,
    `#!/bin/bash
N=$(cat "${stateFile}")
P=$(node -e 'const a=${JSON.stringify(json)}; const i=parseInt(process.argv[1],10); const p=JSON.parse(a)[i]||{stdout:"",exit:0}; console.log(JSON.stringify(p));' $N)
echo $((N+1)) > "${stateFile}"
OUT=$(node -e 'process.stdout.write(JSON.parse(process.argv[1]).stdout||"")' "$P")
ERR=$(node -e 'process.stdout.write(JSON.parse(process.argv[1]).stderr||"")' "$P")
EX=$(node -e 'process.stdout.write(String(JSON.parse(process.argv[1]).exit||0))' "$P")
printf '%s' "$OUT"
if [ -n "$ERR" ]; then printf '%s' "$ERR" 1>&2; fi
exit $EX
`);
  fs.chmodSync(bin, 0o755);
  return { bin, dir };
}

function loadCollectorWithStub(cliPath, upsertStub) {
  process.env.ALPACA_CLI_BIN = cliPath;
  delete require.cache[require.resolve('../src/channels/api/alpaca_cli')];
  delete require.cache[require.resolve('../src/pipeline/store')];
  delete require.cache[require.resolve('../src/pipeline/collector')];
  const store = require('../src/pipeline/store');
  store.upsertPrices = upsertStub;
  return require('../src/pipeline/collector');
}

test('fillPricesAlpaca: parses bars and forwards to upsertPrices', async () => {
  const stdout = JSON.stringify({
    symbol: 'AAPL',
    next_page_token: '',
    bars: [
      { t: '2026-05-22T04:00:00Z', o: 306.12, h: 311.4, l: 305.84, c: 308.82, v: 43778765, vw: 309.15, n: 752162 },
      { t: '2026-05-21T04:00:00Z', o: 301.05, h: 305.54, l: 300.4,  c: 304.99, v: 43130643, vw: 303.96, n: 654038 },
    ],
  });
  const captured = [];
  const collector = loadCollectorWithStub(makeFakeCli(stdout),
    async (ticker, bars, source) => { captured.push({ ticker, bars, source }); return bars.length; });

  const written = await collector.fillPricesAlpaca('AAPL', '2026-05-21', '2026-05-22');
  assert.equal(written, 2, 'returns row count');
  assert.equal(captured.length, 1, 'upsertPrices called once');
  assert.equal(captured[0].ticker, 'AAPL');
  assert.equal(captured[0].source, 'alpaca');
  assert.equal(captured[0].bars.length, 2);
  assert.equal(captured[0].bars[0].c, 308.82);
});

test('fillPricesAlpaca: empty bars array returns 0 without upsertPrices', async () => {
  const stdout = JSON.stringify({ symbol: 'AAPL', next_page_token: '', bars: [] });
  let called = false;
  const collector = loadCollectorWithStub(makeFakeCli(stdout),
    async () => { called = true; return 0; });

  const written = await collector.fillPricesAlpaca('AAPL', '2026-05-22', '2026-05-22');
  assert.equal(written, 0);
  assert.equal(called, false, 'upsertPrices not invoked for empty bars');
});

test('fillPricesAlpaca: null bars (unknown symbol clean response) returns 0', async () => {
  const stdout = JSON.stringify({ symbol: 'NOTREAL', next_page_token: '', bars: null });
  let called = false;
  const collector = loadCollectorWithStub(makeFakeCli(stdout),
    async () => { called = true; return 0; });

  const written = await collector.fillPricesAlpaca('NOTREAL', '2026-05-22', '2026-05-22');
  assert.equal(written, 0);
  assert.equal(called, false);
});

test('fillPricesAlpaca: invalid-symbol 400 is warned and skipped (rc=0)', async () => {
  const errStderr = JSON.stringify({ status: 400, error: 'invalid symbol: ^VIX' });
  const collector = loadCollectorWithStub(
    makeFakeCli('', { stderr: errStderr, exit: 1 }),
    async () => { throw new Error('upsertPrices should not be called on invalid symbol'); },
  );

  const written = await collector.fillPricesAlpaca('^VIX', '2026-05-22', '2026-05-22');
  assert.equal(written, 0);
});

test('fillPricesAlpaca: non-400 errors throw', async () => {
  const errStderr = JSON.stringify({ status: 500, error: 'internal server error' });
  const collector = loadCollectorWithStub(
    makeFakeCli('', { stderr: errStderr, exit: 1 }),
    async () => 0,
  );

  await assert.rejects(
    collector.fillPricesAlpaca('AAPL', '2026-05-22', '2026-05-22'),
    /alpaca data bars AAPL/,
  );
});

test('fillPricesAlpaca: pagination loops until next_page_token empty', async () => {
  const { bin, dir } = makeMultiCli([
    { stdout: JSON.stringify({
        symbol: 'SPY', next_page_token: 'PAGE2',
        bars: [{ t: '2026-05-19T04:00:00Z', o: 740, h: 741, l: 738, c: 740.5, v: 1, vw: 740, n: 1 }],
      }) },
    { stdout: JSON.stringify({
        symbol: 'SPY', next_page_token: '',
        bars: [{ t: '2026-05-20T04:00:00Z', o: 741, h: 742, l: 740, c: 741.5, v: 1, vw: 741, n: 1 }],
      }) },
  ]);
  let pages = 0;
  const collector = loadCollectorWithStub(bin,
    async (_t, bars) => { pages++; return bars.length; });

  const written = await collector.fillPricesAlpaca('SPY', '2026-05-19', '2026-05-20');
  assert.equal(written, 2);
  assert.equal(pages, 2, 'upsertPrices called once per page');

  try { fs.rmSync(dir, { recursive: true, force: true }); } catch (_) {}
});
