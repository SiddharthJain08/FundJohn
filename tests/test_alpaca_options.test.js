'use strict';

/**
 * tests/test_alpaca_options.test.js
 *
 * Unit tests for src/pipeline/alpaca_options.js (Phase 2.1 of alpaca-cli
 * integration). Uses fake-CLI stub binaries so we exercise the helper
 * without live Alpaca calls.
 *
 * Run:
 *   node --test tests/test_alpaca_options.test.js
 */

const { test } = require('node:test');
const assert    = require('node:assert/strict');
const fs        = require('node:fs');
const os        = require('node:os');
const path      = require('node:path');

function makeFakeCli(stdout) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-opt-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(bin,
    `#!/bin/bash\nprintf '%s' ${JSON.stringify(stdout)}\n`);
  fs.chmodSync(bin, 0o755);
  return bin;
}

function loadModule(cliPath) {
  process.env.ALPACA_CLI_BIN = cliPath;
  delete require.cache[require.resolve('../src/channels/api/alpaca_cli')];
  delete require.cache[require.resolve('../src/pipeline/alpaca_options')];
  return require('../src/pipeline/alpaca_options');
}

test('decodeOccSymbol parses standard OCC contract symbols', () => {
  const { decodeOccSymbol } = require('../src/pipeline/alpaca_options');
  assert.deepEqual(
    decodeOccSymbol('AAPL260429C00185000'),
    { root: 'AAPL', expiration: '2026-04-29', optionType: 'call', strike: 185 },
  );
  assert.deepEqual(
    decodeOccSymbol('SPY260620P00450500'),
    { root: 'SPY',  expiration: '2026-06-20', optionType: 'put',  strike: 450.5 },
  );
  assert.equal(decodeOccSymbol('not-occ'),    null);
  assert.equal(decodeOccSymbol(''),           null);
  assert.equal(decodeOccSymbol(null),         null);
});

test('flattenSnapshot maps Alpaca chain payload to row schema', () => {
  const { flattenSnapshot } = require('../src/pipeline/alpaca_options');
  const row = flattenSnapshot('AAPL260429C00185000', {
    latestQuote: { ap: 86.75, as: 16, bp: 82.68, bs: 25,
                   t: '2026-04-28T19:59:59Z' },
    latestTrade: { p: 83.98, t: '2026-04-28T18:58:43Z' },
    greeks: { delta: 0.62, gamma: 0.04, theta: -0.05, vega: 0.12, rho: 0.07 },
  });
  assert.equal(row.contract_symbol, 'AAPL260429C00185000');
  assert.equal(row.underlying,      'AAPL');
  assert.equal(row.expiration,      '2026-04-29');
  assert.equal(row.option_type,     'call');
  assert.equal(row.strike,          185);
  assert.equal(row.bid,             82.68);
  assert.equal(row.ask,             86.75);
  assert.equal(row.last_price,      83.98);
  assert.equal(row.delta,           0.62);
  assert.equal(row.gamma,           0.04);
  assert.equal(row.theta,           -0.05);
  assert.equal(row.vega,            0.12);
  assert.equal(row.rho,             0.07);
  assert.equal(row.implied_volatility, null);  // alpha CLI doesn't surface IV yet
});

test('runOptionsAlpaca shells correct CLI args + parses single page', async () => {
  const stdout = JSON.stringify({
    next_page_token: null,
    snapshots: {
      'AAPL260429C00185000': {
        latestQuote: { ap: 86.75, bp: 82.68 },
        latestTrade: { p: 83.98 },
        greeks: { delta: 0.62, gamma: 0.04, theta: -0.05, vega: 0.12, rho: 0.07 },
      },
      'AAPL260429P00185000': {
        latestQuote: { ap: 1.10,  bp: 1.05  },
        latestTrade: { p: 1.08 },
        greeks: { delta: -0.38, gamma: 0.04, theta: -0.05, vega: 0.12, rho: -0.03 },
      },
    },
  });
  const bin = makeFakeCli(stdout);
  const { runOptionsAlpaca } = loadModule(bin);
  const rows = await runOptionsAlpaca('AAPL', { expirationGte: '2026-04-29' });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].underlying, 'AAPL');
  assert.equal(rows[0].option_type, 'call');
  assert.equal(rows[1].option_type, 'put');
  assert.equal(rows[1].delta, -0.38);
});

test('runOptionsAlpaca pages through next_page_token', async () => {
  // Build a CLI that emits page 1 then page 2 based on whether --page-token is in argv.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-page-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(bin, `#!/bin/bash
if [[ " $* " == *" --page-token "* ]]; then
  printf '%s' '{"next_page_token":null,"snapshots":{"AAPL260429C00200000":{"greeks":{"delta":0.4}}}}'
else
  printf '%s' '{"next_page_token":"PAGE2TOKEN","snapshots":{"AAPL260429C00185000":{"greeks":{"delta":0.62}}}}'
fi
`);
  fs.chmodSync(bin, 0o755);
  const { runOptionsAlpaca } = loadModule(bin);
  const rows = await runOptionsAlpaca('AAPL');
  assert.equal(rows.length, 2);
  assert.equal(rows[0].contract_symbol, 'AAPL260429C00185000');
  assert.equal(rows[1].contract_symbol, 'AAPL260429C00200000');
});

test('runOptionsAlpaca propagates CLI errors as thrown Error', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-err-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(bin, `#!/bin/bash
printf '%s' '{"status":403,"error":"options data not enabled for this account"}' >&2
exit 1
`);
  fs.chmodSync(bin, 0o755);
  const { runOptionsAlpaca } = loadModule(bin);
  await assert.rejects(
    () => runOptionsAlpaca('AAPL'),
    (err) => err.cliError && err.cliError.status === 403,
  );
});
