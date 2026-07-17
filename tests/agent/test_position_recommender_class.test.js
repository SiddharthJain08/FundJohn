/**
 * SP-4: _classForStrategyFromManifest reads instrument_class from the manifest (default equity).
 * Run: node --test tests/test_position_recommender_class.test.js
 */
process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';
const { test } = require('node:test');
const assert   = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { _classForStrategyFromManifest } = require('../../src/agent/curators/position_recommender');

test('reads declared instrument_class', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pr-'));
  const mf = path.join(dir, 'manifest.json');
  fs.writeFileSync(mf, JSON.stringify({ strategies: { S_a: { instrument_class: 'crypto' } } }));
  assert.equal(_classForStrategyFromManifest(mf, 'S_a'), 'crypto');
});

test('defaults equity when absent', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pr-'));
  const mf = path.join(dir, 'manifest.json');
  fs.writeFileSync(mf, JSON.stringify({ strategies: { S_a: {} } }));
  assert.equal(_classForStrategyFromManifest(mf, 'S_a'), 'equity');
  assert.equal(_classForStrategyFromManifest(mf, 'S_missing'), 'equity');
});
