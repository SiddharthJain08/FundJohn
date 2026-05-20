'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const fs       = require('node:fs');

const ROOT = path.resolve(__dirname, '..');

test('position_recommender.js requires _critique_eligibility and synthesizer modules', () => {
  const src = fs.readFileSync(
    path.join(ROOT, 'src/agent/curators/position_recommender.js'),
    'utf8'
  );
  assert.ok(src.includes("require('./_critique_eligibility')") ||
            src.includes("require('./_critique_eligibility.js')"),
            'must require _critique_eligibility');
  assert.ok(src.includes("require('./synthesizer')") ||
            src.includes("require('./synthesizer.js')"),
            'must require synthesizer');
});

test('position_recommender exposes _sourceRecommendedSize helper for testability', () => {
  const mod = require(path.join(ROOT, 'src/agent/curators/position_recommender.js'));
  assert.equal(typeof mod._sourceRecommendedSize, 'function');
});

test('_sourceRecommendedSize prefers strategy_synthesis row when present', () => {
  const mod = require(path.join(ROOT, 'src/agent/curators/position_recommender.js'));
  const synthRow = { adjusted_recommended_size_pct: 0.024 };
  const memoRec  = { recommended_size_pct: 0.030 };
  assert.equal(mod._sourceRecommendedSize(synthRow, memoRec), 0.024);
});

test('_sourceRecommendedSize falls back to memo when no synthesis row', () => {
  const mod = require(path.join(ROOT, 'src/agent/curators/position_recommender.js'));
  const memoRec = { recommended_size_pct: 0.030 };
  assert.equal(mod._sourceRecommendedSize(null, memoRec), 0.030);
});

test('_runSynthesisIfEligible signature accepts deltasRecSize as positional parameter', () => {
  // Static check: read the file and verify the signature shape
  const src = require('node:fs').readFileSync(
    path.join(ROOT, 'src/agent/curators/position_recommender.js'),
    'utf8'
  );
  // Either compact one-line signature or multi-line — must contain deltasRecSize
  assert.match(
    src,
    /_runSynthesisIfEligible\s*\(\s*memo\s*,\s*deltasRecSize\s*,\s*weekOf\s*\)/,
    '_runSynthesisIfEligible must accept (memo, deltasRecSize, weekOf)'
  );
  // The call site in run() must pass deltas.recommended_size_pct as the second arg
  assert.match(
    src,
    /_runSynthesisIfEligible\s*\(\s*memo\s*,\s*deltas\.recommended_size_pct\s*,\s*weekOf\s*\)/,
    'call site must pass deltas.recommended_size_pct'
  );
});
