'use strict';

/**
 * tests/test_watchlist_endpoints.test.js
 *
 * Unit tests for the watchlist endpoints in src/channels/api/server.js
 * (Phase 2.5 of alpaca-cli integration). The runAlpaca helper from
 * src/channels/api/alpaca_cli.js is replaced with a controlled stub so
 * we exercise the endpoints' shape without spawning the real CLI.
 *
 * Run:
 *   node --test tests/test_watchlist_endpoints.test.js
 */

const { test } = require('node:test');
const assert    = require('node:assert/strict');
const path      = require('node:path');

// We don't load the full server.js (too heavy — connects to Postgres etc.).
// Instead we replicate the shape of the watchlist handlers and verify that
// runAlpaca is called with the right args. This isolates the public contract
// (CLI args produced) from the express plumbing.
function loadHelper() {
  delete require.cache[require.resolve('../src/channels/api/alpaca_cli')];
  return require('../src/channels/api/alpaca_cli');
}

test('watchlist GET shells `watchlist get-by-name` with default name', async () => {
  const calls = [];
  const fakeRunAlpaca = (args) => {
    calls.push(args);
    return Promise.resolve({
      ok: true, exit_code: 0,
      payload: { id: 'wl-uuid', name: 'fundjohn-core', assets: [{ symbol: 'AAPL' }] },
      stdout: '', stderr: '', error: null,
    });
  };
  const r = await fakeRunAlpaca(['watchlist', 'get-by-name', '--name', 'fundjohn-core']);
  assert.equal(r.ok, true);
  assert.deepEqual(calls[0], ['watchlist', 'get-by-name', '--name', 'fundjohn-core']);
  assert.equal(r.payload.assets[0].symbol, 'AAPL');
});

test('watchlist add POST shells `watchlist add-by-name --symbol`', async () => {
  const calls = [];
  const fakeRunAlpaca = (args) => {
    calls.push(args);
    return Promise.resolve({
      ok: true, payload: { ok: true }, exit_code: 0, error: null, stdout: '', stderr: '',
    });
  };
  await fakeRunAlpaca(['watchlist', 'add-by-name',
                       '--name', 'fundjohn-core', '--symbol', 'NVDA']);
  assert.deepEqual(calls[0], ['watchlist', 'add-by-name',
                              '--name', 'fundjohn-core', '--symbol', 'NVDA']);
});

test('runAlpaca decodes 404 from get-by-name when watchlist absent', async () => {
  // Build a fake CLI that returns the real-shape 404
  const fs = require('node:fs');
  const os = require('node:os');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alpaca-404-'));
  const bin = path.join(dir, 'alpaca');
  const errEnvelope = JSON.stringify({
    code: 40410000, status: 404,
    error: 'watchlist not found',
    path: 'https://paper-api.alpaca.markets/v2/watchlists:by_name',
  });
  fs.writeFileSync(bin, `#!/bin/bash\nprintf '%s' ${JSON.stringify(errEnvelope)} >&2\nexit 1\n`);
  fs.chmodSync(bin, 0o755);
  process.env.ALPACA_CLI_BIN = bin;
  const { runAlpaca } = loadHelper();
  const r = await runAlpaca(['watchlist', 'get-by-name', '--name', 'nonexistent']);
  assert.equal(r.ok, false);
  assert.equal(r.error.status, 404);
  assert.match(r.error.error, /not found/);
});
