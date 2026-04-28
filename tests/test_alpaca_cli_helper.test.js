'use strict';

/**
 * tests/test_alpaca_cli_helper.test.js
 *
 * Unit tests for src/channels/api/alpaca_cli.js — the runAlpaca helper
 * that every dashboard + collector code path uses to shell into the CLI.
 *
 * Tests use a fake CLI binary (a tiny shell script) to simulate the
 * three behaviors the helper must decode: success-with-JSON, error-with-
 * JSON-on-stderr, and timeout. No live Alpaca calls.
 *
 * Run:
 *   node --test tests/test_alpaca_cli_helper.test.js
 */

const { test } = require('node:test');
const assert    = require('node:assert/strict');
const fs        = require('node:fs');
const os        = require('node:os');
const path      = require('node:path');

// Build a synthetic CLI binary that prints fixed stdout/stderr + exit code.
function makeFakeCli(stdout, stderr, exitCode) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fake-alpaca-'));
  const bin = path.join(dir, 'alpaca');
  // bash heredoc: encode stdout + stderr verbatim
  const stdoutLines = JSON.stringify(stdout);
  const stderrLines = JSON.stringify(stderr);
  fs.writeFileSync(bin, `#!/bin/bash
printf '%s' ${stdoutLines}
printf '%s' ${stderrLines} >&2
exit ${exitCode}
`);
  fs.chmodSync(bin, 0o755);
  return bin;
}

function loadHelper(cliPath) {
  // Reset module cache so each test gets a fresh ALPACA_CLI binding
  process.env.ALPACA_CLI_BIN = cliPath;
  delete require.cache[require.resolve('../src/channels/api/alpaca_cli')];
  return require('../src/channels/api/alpaca_cli');
}

test('runAlpaca decodes success → ok:true, parsed payload', async () => {
  const accountJson = { equity: '100000', cash: '50000', buying_power: '150000' };
  const bin = makeFakeCli(JSON.stringify(accountJson), '', 0);
  const { runAlpaca } = loadHelper(bin);
  const r = await runAlpaca(['account', 'get']);
  assert.equal(r.ok, true);
  assert.equal(r.exit_code, 0);
  assert.deepEqual(r.payload, accountJson);
  assert.equal(r.error, null);
});

test('runAlpaca decodes error → ok:false, error envelope from stderr', async () => {
  const errJson = {
    code: 42210000, status: 422,
    error: 'asset "XYZINVALID" not found',
    path: 'https://paper-api.alpaca.markets/v2/orders',
  };
  const bin = makeFakeCli('', JSON.stringify(errJson), 1);
  const { runAlpaca } = loadHelper(bin);
  const r = await runAlpaca(['order', 'submit', '--symbol', 'XYZINVALID',
                             '--side', 'buy', '--qty', '1', '--type', 'market']);
  assert.equal(r.ok, false);
  assert.equal(r.exit_code, 1);
  assert.equal(r.payload, null);
  assert.equal(r.error.status, 422);
  assert.match(r.error.error, /XYZINVALID/);
});

test('runAlpaca handles non-JSON stdout (returns raw string in payload)', async () => {
  const bin = makeFakeCli('plain text not json', '', 0);
  const { runAlpaca } = loadHelper(bin);
  const r = await runAlpaca(['version']);
  assert.equal(r.ok, true);
  assert.equal(r.payload, 'plain text not json');
});

test('runAlpaca times out long-running subprocess', async () => {
  // Build a sleep-forever CLI: the helper's 200ms timeout should kill it.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'slow-alpaca-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(bin, '#!/bin/bash\nsleep 30\n');
  fs.chmodSync(bin, 0o755);
  const { runAlpaca } = loadHelper(bin);
  const t0 = Date.now();
  const r = await runAlpaca(['account', 'get'], { timeout: 200 });
  const elapsed = Date.now() - t0;
  assert.equal(r.ok, false);
  assert.equal(r.error.error, 'cli timeout');
  assert.ok(elapsed < 5_000, `should kill subprocess promptly, elapsed=${elapsed}ms`);
});

test('runAlpaca passes argv through verbatim', async () => {
  // CLI script that echoes its argv as JSON — proves args reach the binary unchanged.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'echo-alpaca-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(bin, `#!/bin/bash
python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" "$@"
`);
  fs.chmodSync(bin, 0o755);
  const { runAlpaca } = loadHelper(bin);
  const args = ['order', 'submit', '--symbol', 'AAPL', '--side', 'buy',
                '--qty', '10', '--type', 'market', '--time-in-force', 'day',
                '--client-order-id', 'TEST_ABC123'];
  const r = await runAlpaca(args);
  assert.equal(r.ok, true);
  assert.deepEqual(r.payload, args);
});
