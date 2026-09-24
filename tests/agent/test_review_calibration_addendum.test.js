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

test('_renderProposalCalibration names the exact scored field and evidence-cap rule', () => {
  const out = _renderProposalCalibration(CAL);
  // T6 ledger row: "per-bucket match rates, evidence level/cap". Only
  // regime_recommendations[].confidence is ever written to
  // strategy_regime_param_proposals (see :637-650) and scored into
  // mastermind_proposal_outcomes — the top-level recommendations.confidence
  // is never persisted there, so the block must name the field precisely
  // rather than a generic "confidence you emit".
  assert.ok(out.includes('regime_recommendations'), 'must name the exact scored field');
  // Evidence cap tiers/thresholds mirrored from mastermind_calibration.py
  // EVIDENCE_CAPS / evidence_level().
  assert.ok(out.includes('0.35'), 'the "none" tier cap must appear');
  assert.ok(out.includes('45 days'), 'the staleness drop-one-tier rule must appear');
  assert.ok(out.includes('OPENCLAW_PROPOSAL_CALIBRATED'), 'must name the flag that enforces the cap');
});

test('_renderProposalCalibration renders the exact pinned block for the CAL fixture', () => {
  const expected =
    '--- CONFIDENCE CALIBRATION (your own track record) ---\n' +
    '\n' +
    'The `confidence` field of each entry in `regime_recommendations` (schema\n' +
    'above) is scored 30 days later against the live Sharpe direction for that\n' +
    '(strategy, regime). This is how those scores came out:\n' +
    '\n' +
    '  bucket        n    matched  match_rate\n' +
    '  [0.0, 0.2]      0        0  n/a (thin sample)\n' +
    '  [0.2, 0.4]      2        1  n/a (thin sample)\n' +
    '  [0.4, 0.6]      5        2  n/a (thin sample)\n' +
    '  [0.6, 0.8]     16        9  0.563\n' +
    '  [0.8, 1.0]     18       10  0.556\n' +
    '\n' +
    '  Brier score: 0.254 (warn >= 0.10, fail >= 0.20)\n' +
    '  Overall hit rate: 0.66 against mean stated confidence 0.75 (41 resolved of 96)\n' +
    '\n' +
    '  auto_approve can ALSO cap your confidence by how much LIVE evidence\n' +
    '  backs the specific (strategy, regime) decision at approval time — a\n' +
    '  trailing 30-day closed-trade count, staleness-adjusted:\n' +
    '    <10 closed trades  -> "none"   cap 0.35\n' +
    '    <30 closed trades  -> "low"    cap 0.55\n' +
    '    <100 closed trades -> "medium" cap 0.75\n' +
    '    >=100 closed trades -> "high"  cap 1.0\n' +
    '  (drop one tier if the most recent closed trade is >45 days old).\n' +
    '  When OPENCLAW_PROPOSAL_CALIBRATED=1, auto_approve compares\n' +
    '  min(calibrated_confidence, cap) against the 0.85 floor, so a\n' +
    '  thin-evidence regime cannot earn approval on confidence alone. While\n' +
    '  the flag is unset (today\'s default), the floor compare uses your raw\n' +
    '  stated confidence only — the cap is computed and logged, not enforced.\n' +
    '\n' +
    'You are OVER-CONFIDENT: your stated confidence exceeds your realised hit rate.\n' +
    'A bucket whose match_rate sits well below its own midpoint is one you should stop\n' +
    'using — move those calls down a bucket. Reserve >= 0.8 for recommendations you\n' +
    'would defend on the trade-level numbers alone, not on the shape of the story.\n' +
    'Ignore "thin sample" buckets until they accumulate enough observations.\n';
  assert.equal(_renderProposalCalibration(CAL), expected);
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
