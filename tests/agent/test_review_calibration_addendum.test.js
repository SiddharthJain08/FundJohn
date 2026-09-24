'use strict';

/**
 * D2c — the calibration addendum is back in the sizing-proposal prompt
 * (spec 2026-09-12 §4 D2; removed 2026-05-19, see the note at :274).
 *
 * Pure string assertions on buildStrategyPrompt / _renderProposalCalibration —
 * no DB, no real spawn, no LLM. `_loadProposalCalibration`'s fail-open path is
 * exercised below via its injected `spawn` parameter (a fake), never a real
 * `spawnSync` call.
 *
 * Run:
 *   cd /root/openclaw/.claude/worktrees/qd-adoptions && \
 *     node --test tests/agent/test_review_calibration_addendum.test.js
 */

const { test } = require('node:test');
const assert   = require('node:assert/strict');

const {
  buildStrategyPrompt,
  _renderProposalCalibration,
  _loadProposalCalibration,
} = require('../../src/agent/curators/comprehensive_review');

const STRATEGY = {
  id: 'S_demo', name: 'Demo', status: 'live', tier: 2,
  instrument_class: 'equity', universe: ['AAPL'], signal_frequency: 'daily',
  parameters: {}, regime_conditions: {}, created_at: '2026-01-01',
};
const TRADE_PACK = { signals: [], pnl: [], oue: {} };
const COUNTERFACTUALS = { base: {} };

const CAL = {
  total_observations: 96,
  resolved_observations: 41,
  hit_rate: 0.66,
  mean_confidence: 0.75,
  brier_score: 0.254,
  buckets: [
    { range: '[0.0, 0.2]', count: 0,  matched: 0,  match_rate: null },
    { range: '[0.2, 0.4]', count: 2,  matched: 1,  match_rate: 0.5 },
    { range: '[0.4, 0.6]', count: 5,  matched: 2,  match_rate: 0.4 },
    { range: '[0.6, 0.8]', count: 16, matched: 9,  match_rate: 0.5625 },
    { range: '[0.8, 1.0]', count: 18, matched: 10, match_rate: 0.5555555 },
  ],
};

test('_renderProposalCalibration renders every bucket row verbatim', () => {
  const out = _renderProposalCalibration(CAL);
  for (const b of CAL.buckets) {
    assert.ok(out.includes(b.range), `bucket ${b.range} missing from the block`);
  }
  assert.ok(out.includes('0.556') || out.includes('0.5556'), 'the [0.8,1.0] match rate must appear');
  assert.ok(out.includes('18'), 'the [0.8,1.0] sample size must appear');
});

test('_renderProposalCalibration surfaces Brier, hit rate and mean confidence', () => {
  const out = _renderProposalCalibration(CAL);
  assert.ok(out.includes('0.254'), 'Brier score');
  assert.ok(out.includes('0.66'),  'hit rate');
  assert.ok(out.includes('0.75'),  'mean stated confidence');
});

test('_renderProposalCalibration tells the model what to do about it', () => {
  const out = _renderProposalCalibration(CAL).toLowerCase();
  assert.ok(out.includes('over-confident') || out.includes('overconfident'));
  assert.ok(out.includes('confidence'));
});

test('_renderProposalCalibration is empty for null / cold start', () => {
  assert.equal(_renderProposalCalibration(null), '');
  assert.equal(_renderProposalCalibration(undefined), '');
  assert.equal(_renderProposalCalibration({ buckets: [] }), '');
});

test('buildStrategyPrompt embeds the block when calibration is supplied', () => {
  const prompt = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS, CAL);
  assert.ok(prompt.includes('CONFIDENCE CALIBRATION'), 'block header present');
  assert.ok(prompt.includes('[0.8, 1.0]'));
  assert.ok(prompt.includes('0.254'));
});

test('buildStrategyPrompt is unchanged when calibration is omitted', () => {
  const withNull = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS, null);
  const legacy   = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS);
  assert.equal(withNull, legacy);
  assert.ok(!legacy.includes('CONFIDENCE CALIBRATION'));
});

test('_renderProposalCalibration skips a malformed bucket entry instead of throwing', () => {
  const malformed = { ...CAL, buckets: [null, undefined, CAL.buckets[4]] };
  const out = _renderProposalCalibration(malformed);
  assert.ok(out.includes('[0.8, 1.0]'), 'the one well-formed bucket still renders');
  assert.ok(out.includes('18'));
});

test('buildStrategyPrompt never throws even if calibration render blows up', () => {
  // A bucket array whose element access itself throws — defeats
  // _renderProposalCalibration's own null/typeof guards (they never get to
  // read the throwing property), so this actually exercises
  // buildStrategyPrompt's outer try/catch, not just the inner filter.
  const throwingBuckets = [];
  throwingBuckets.length = 1;
  Object.defineProperty(throwingBuckets, '0', {
    get() { throw new Error('boom'); }, enumerable: true,
  });
  const hostile = { buckets: throwingBuckets };

  // Sanity: confirm this construction really does throw inside the renderer
  // directly, so the assertion below is proving the outer catch, not a
  // no-op.
  assert.throws(() => _renderProposalCalibration(hostile));

  let prompt;
  assert.doesNotThrow(() => {
    prompt = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS, hostile);
  });
  assert.ok(!prompt.includes('CONFIDENCE CALIBRATION'), 'render failure omits the block, not a partial one');
});

test('_loadProposalCalibration fails open (returns null) on every spawn failure mode', () => {
  const cases = [
    () => ({ status: 1, stdout: '' }),                          // non-zero exit
    () => ({ status: 0, stdout: '' }),                           // empty stdout
    () => ({ status: 0, stdout: 'not json' }),                   // unparseable
    () => ({ status: 0, stdout: JSON.stringify({ buckets: 'x' }) }), // buckets not an array
    () => { throw new Error('spawn ENOENT'); },                  // spawn itself throws
  ];
  for (const spawn of cases) {
    const result = _loadProposalCalibration({ spawn });
    assert.equal(result, null);
  }
});

test('_loadProposalCalibration parses a well-formed report from an injected spawn', () => {
  const spawn = () => ({ status: 0, stdout: JSON.stringify(CAL) });
  const result = _loadProposalCalibration({ spawn });
  assert.deepEqual(result, CAL);
});

test('fail-open proof: buildStrategyPrompt with a failing injected loader equals the no-calibration prompt', () => {
  const failingSpawn = () => { throw new Error('spawn ENOENT'); };
  const cal = _loadProposalCalibration({ spawn: failingSpawn });
  assert.equal(cal, null, 'loader must fail open to null');
  const withFailedLoader = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS, cal);
  const legacy = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS);
  assert.equal(withFailedLoader, legacy,
    'a failed calibration load must render a prompt byte-identical to the no-calibration prompt');
});

test('the 2026-05-19 removal note no longer claims the addenda logic is gone', () => {
  const src = require('node:fs').readFileSync(
    require('node:path').join(__dirname, '../../src/agent/curators/comprehensive_review.js'),
    'utf8');
  assert.ok(!src.includes('Phase 2F calibration-addenda prepend logic removed'),
    'the stale removal note must be replaced by the restored block');
  assert.ok(src.includes('_renderProposalCalibration'));
});
