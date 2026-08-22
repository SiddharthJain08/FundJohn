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
  delete require.cache[require.resolve('../../src/channels/api/alpaca_cli')];
  return require('../../src/channels/api/alpaca_cli');
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
  const r = await runAlpaca(args, { timeout: 10_000 });
  assert.equal(r.ok, true);
  // Every call carries the CLI's global --quiet + --timeout (sec = ms/1000 - 1)
  // appended AFTER the caller's argv — the caller's flags are untouched.
  assert.deepEqual(r.payload, [...args, '--quiet', '--timeout', '9']);
});

test('runAlpaca does not duplicate --quiet/--timeout the caller already passed', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'echo2-alpaca-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(bin, `#!/bin/bash
python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" "$@"
`);
  fs.chmodSync(bin, 0o755);
  const { runAlpaca, cliGlobalFlags } = loadHelper(bin);
  const args = ['order', 'list', '-q', '--timeout', '3'];
  const r = await runAlpaca(args);
  assert.deepEqual(r.payload, args);
  assert.deepEqual(cliGlobalFlags(['clock'], 30_000), ['--quiet', '--timeout', '29']);
  assert.deepEqual(cliGlobalFlags(['clock'], 500), ['--quiet', '--timeout', '1']);
});

test('runAlpaca surfaces exit code 2 as auth_error (never retry; fix credentials)', async () => {
  const errJson = { code: 40110000, status: 401, error: 'unauthorized', hint: 'check keys' };
  // v0.0.10+ pretty-prints the stderr document across lines — parse as a whole.
  // (bash printf would mangle the newlines, so this fake is a python one-liner.)
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'auth-alpaca-'));
  const bin = path.join(dir, 'alpaca');
  const b64 = Buffer.from(JSON.stringify(errJson, null, 2)).toString('base64');
  fs.writeFileSync(bin, `#!/bin/bash
python3 -c "import sys,base64; sys.stderr.write(base64.b64decode('${b64}').decode()); sys.exit(2)"
`);
  fs.chmodSync(bin, 0o755);
  const { runAlpaca } = loadHelper(bin);
  const r = await runAlpaca(['account', 'get']);
  assert.equal(r.ok, false);
  assert.equal(r.exit_code, 2);
  assert.equal(r.auth_error, true);
  assert.equal(r.error.status, 401);
  const bin1 = makeFakeCli('', JSON.stringify({ status: 404, error: 'nope' }), 1);
  const r1 = await loadHelper(bin1).runAlpaca(['order', 'get', '--order-id', 'x']);
  assert.equal(r1.auth_error, false);
});
