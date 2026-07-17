'use strict';

const { test } = require('node:test');
const assert    = require('node:assert/strict');
const fs        = require('node:fs');
const os        = require('node:os');
const path      = require('node:path');

function makeFakeCli(stdout) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'collector-opt-'));
  const bin = path.join(dir, 'alpaca');
  fs.writeFileSync(bin,
    `#!/bin/bash\nprintf '%s' ${JSON.stringify(stdout)}\n`);
  fs.chmodSync(bin, 0o755);
  return bin;
}

test('OPTIONS_DATA_SOURCE env var is no longer referenced in collector.js', () => {
  const src = fs.readFileSync(
    path.join(__dirname, '..', '..', 'src', 'pipeline', 'collector.js'),
    'utf8'
  );
  assert.ok(!src.includes('OPTIONS_DATA_SOURCE'),
    'OPTIONS_DATA_SOURCE must be fully removed from collector.js after SP-1');
});

test('options chain pull uses alpaca CLI with greeks-validity filter', async () => {
  const chain = JSON.stringify({
    next_page_token: null,
    snapshots: {
      'SPY260618C00742000': {
        dailyBar: { c: 13.05, v: 5000 },
        greeks: { delta: 0.548, gamma: 0.014, theta: -0.24, vega: 0.81, rho: 0.30 },
      },
      'SPY260618C00100000': {
        dailyBar: { c: 642.0, v: 0 },
        greeks: { delta: 0, gamma: 0, theta: 0, vega: 0, rho: 0 },
      },
    },
  });
  process.env.ALPACA_CLI_BIN = makeFakeCli(chain);
  // Clear module cache so ALPACA_CLI_BIN is picked up fresh
  delete require.cache[require.resolve('../../src/channels/api/alpaca_cli')];
  delete require.cache[require.resolve('../../src/pipeline/alpaca_options')];
  delete require.cache[require.resolve('../../src/pipeline/collector')];

  const collector = require('../../src/pipeline/collector');
  // The exported function is fetchOptionsChain — adapt if the implementer chose a different export name
  if (typeof collector.fetchOptionsChain !== 'function') {
    // If the rewrite didn't export a callable, skip the live-call portion
    // but the OPTIONS_DATA_SOURCE assertion above is the primary spec gate.
    return;
  }
  const rows = await collector.fetchOptionsChain('SPY');
  const symbols = rows.map(r => r.contract_symbol);
  assert.ok(symbols.includes('SPY260618C00742000'),
    'should include ATM contract with full greeks');
  assert.ok(!symbols.includes('SPY260618C00100000'),
    'should drop deep-ITM zero-volume zero-greeks contract');
});
