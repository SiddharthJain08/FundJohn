'use strict';

/**
 * D2c — the calibration addendum is back in the sizing-proposal prompt
 * (spec 2026-09-12 §4 D2; removed 2026-05-19, see the note at :274).
 *
 * D6 fix1 (this file, expanded): the auto-approve floor must be read LIVE
 * from env at render time (not hardcoded 0.85), loader failures must be
 * visible (console.warn), a deflation-only guarantee sentence was added,
 * the per-bucket thin-sample cutoff moved to MIN_BUCKET_N=8, and the
 * loader is now hoisted into run() (called once per Saturday review, not
 * once per strategy).
 *
 * Pure string assertions on buildStrategyPrompt / _renderProposalCalibration
 * / _loadProposalCalibration / run() — no DB, no real spawn, no LLM.
 * `_loadProposalCalibration`'s fail-open path is exercised below via its
 * injected `spawn` parameter (a fake), never a real `spawnSync` call.
 * `run()`'s DB/LLM dependencies (`fetchStrategies` / `reviewOne`) are
 * likewise injected fakes.
 *
 * Run:
 *   cd /root/openclaw/.claude/worktrees/qd-adoptions && \
 *     nice -n 19 node --test tests/agent/test_review_calibration_addendum.test.js
 */

const { test, beforeEach, afterEach } = require('node:test');
const assert   = require('node:assert/strict');
const fs       = require('node:fs');
const path     = require('node:path');

const {
  run,
  buildStrategyPrompt,
  _renderProposalCalibration,
  _loadProposalCalibration,
} = require('../../src/agent/curators/comprehensive_review');

const SRC_PATH = path.join(__dirname, '../../src/agent/curators/comprehensive_review.js');

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

// D6 fix1 item 1: the rendered floor depends on
// OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE, which this box's tests may
// inherit set from the real .env (production sets it to 0.9 — see
// src/strategies/proposal_manager.py's docstring). Every test in this file
// must see a deterministic, UNSET floor env unless it explicitly sets its
// own value, so every substring/pinned assertion below that doesn't care
// about the floor still passes regardless of ambient state.
const FLOOR_ENV_KEY = 'OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE';
let _savedFloorEnv;
beforeEach(() => {
  _savedFloorEnv = process.env[FLOOR_ENV_KEY];
  delete process.env[FLOOR_ENV_KEY];
});
afterEach(() => {
  if (_savedFloorEnv === undefined) delete process.env[FLOOR_ENV_KEY];
  else process.env[FLOOR_ENV_KEY] = _savedFloorEnv;
});

function calWithSingleBucketCount(n) {
  return {
    total_observations: n, resolved_observations: n,
    hit_rate: 0.5, mean_confidence: 0.5, brier_score: 0.1,
    buckets: [
      { range: '[0.0, 0.2]', count: 0, matched: 0, match_rate: null },
      { range: '[0.2, 0.4]', count: 0, matched: 0, match_rate: null },
      { range: '[0.4, 0.6]', count: 0, matched: 0, match_rate: null },
      { range: '[0.6, 0.8]', count: n, matched: Math.floor(n / 2), match_rate: 0.5 },
      { range: '[0.8, 1.0]', count: 0, matched: 0, match_rate: null },
    ],
  };
}

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

// D6 fix1 item 1 (IMPORTANT): the floor is the LIVE floor.
test('the auto-approve floor is read from env at RENDER time, worded exactly per the fix1 brief', () => {
  process.env[FLOOR_ENV_KEY] = '0.9';
  const withOverride = _renderProposalCalibration(CAL);
  assert.ok(
    withOverride.includes(
      'the auto-approve floor (0.9, from OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE; code default 0.85)'
    ),
    'must render the LIVE env override (0.9), not a hardcoded 0.85'
  );

  delete process.env[FLOOR_ENV_KEY];
  const withoutOverride = _renderProposalCalibration(CAL);
  assert.ok(
    withoutOverride.includes(
      'the auto-approve floor (0.85, from OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE; code default 0.85)'
    ),
    'unset env must fall back to the code default 0.85 — and this is rendered ' +
    'in the SAME process right after the 0.9 case above, proving the value is ' +
    're-read at render time rather than cached from import time'
  );
});

test('a blank/whitespace auto-approve floor env value falls back to the code default (not Number("")===0)', () => {
  process.env[FLOOR_ENV_KEY] = '   ';
  const out = _renderProposalCalibration(CAL);
  assert.ok(out.includes('code default 0.85'));
  assert.ok(!out.includes('floor (0,'), 'must not silently render Number("")===0 as the floor');
});

test('an unparseable auto-approve floor env value falls back to the code default rather than rendering NaN', () => {
  process.env[FLOOR_ENV_KEY] = 'not-a-number';
  const out = _renderProposalCalibration(CAL);
  assert.ok(out.includes('the auto-approve floor (0.85, from OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE; code default 0.85)'));
  assert.ok(!out.includes('NaN'));
});

// D6 fix1 item 3 (IMPORTANT): deflation-only sentence.
test('the block states the deflation-only guarantee verbatim', () => {
  const out = _renderProposalCalibration(CAL);
  assert.ok(out.includes(
    "A bucket's match rate can only LOWER your stated confidence toward that rate — it never raises it."
  ), 'the exact deflation-only sentence from the fix1 brief must appear');
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
    '  min(calibrated_confidence, cap) against the auto-approve floor (0.85, from OPENCLAW_PROPOSAL_AUTOAPPROVE_MIN_CONFIDENCE; code default 0.85), so a thin-evidence\n' +
    '  regime cannot earn approval on confidence alone. While the flag is\n' +
    '  unset (today\'s default), the floor compare uses your raw stated\n' +
    '  confidence only — the cap is computed and logged, not enforced.\n' +
    '  A bucket\'s match rate can only LOWER your stated confidence toward that rate — it never raises it.\n' +
    '\n' +
    'You are OVER-CONFIDENT: your stated confidence exceeds your realised hit rate.\n' +
    'A bucket whose match_rate sits well below its own midpoint is one you should stop\n' +
    'using — move those calls down a bucket. Reserve >= 0.8 for recommendations you\n' +
    'would defend on the trade-level numbers alone, not on the shape of the story.\n' +
    'Ignore "thin sample" buckets until they accumulate enough observations.\n';
  // Env is guaranteed unset here (beforeEach), so the floor renders as the
  // code default 0.85 — pinned exact-equality, not substring, so any future
  // format drift (including a floor-wording regression) fails this test.
  assert.equal(_renderProposalCalibration(CAL), expected);
});

test('_renderProposalCalibration is empty for null / cold start', () => {
  assert.equal(_renderProposalCalibration(null), '');
  assert.equal(_renderProposalCalibration(undefined), '');
  assert.equal(_renderProposalCalibration({ buckets: [] }), '');
});

// D6 fix1 minor 5b: the real cold-start shape calibration_report() returns
// (src/metrics/mastermind_calibration.py:435-459) before any proposal has
// been decided — 5 bucket rows, every aggregate null/0, not the abbreviated
// `{ buckets: [] }` shape above.
test('the real cold-start shape (5 bucket rows, all count:0, null aggregates) renders no block', () => {
  const coldStart = {
    total_observations: 0,
    resolved_observations: 0,
    hit_rate: null,
    mean_confidence: null,
    brier_score: null,
    buckets: [
      { range: '[0.0, 0.2]', count: 0, matched: 0, match_rate: null },
      { range: '[0.2, 0.4]', count: 0, matched: 0, match_rate: null },
      { range: '[0.4, 0.6]', count: 0, matched: 0, match_rate: null },
      { range: '[0.6, 0.8]', count: 0, matched: 0, match_rate: null },
      { range: '[0.8, 1.0]', count: 0, matched: 0, match_rate: null },
    ],
  };
  assert.equal(_renderProposalCalibration(coldStart), '');
  assert.equal(
    buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS, coldStart),
    buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS)
  );
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
  // D6 fix1 minor 5a: pin the exact pre-diff prompt seam (verified against
  // the pre-D2c source at commit d3989fbf^ — MEMO_SYSTEM_PREAMBLE followed
  // by a blank line then "Strategy: ") so a future interpolation edit to
  // buildStrategyPrompt's template cannot silently change the legacy
  // (calibration-omitted) prompt without failing this test.
  assert.ok(
    legacy.includes('Every claim must cite a number from the data.\n\n\nStrategy: S_demo (Demo)\n'),
    'the legacy prompt seam (preamble -> blank line -> "Strategy: ") must stay byte-identical to pre-D2c'
  );
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

// D6 fix1 item 4 (RULED): thin-sample cutoff is MIN_BUCKET_N=8, per-bucket
// (mastermind_calibration.py's own floor for calibrated_confidence()), not
// doctor.py's CALIBRATION_MIN_SAMPLES=10 (a global total).
test('MIN_BUCKET_N=8: a bucket with exactly 8 observations is rendered, not thin', () => {
  const out = _renderProposalCalibration(calWithSingleBucketCount(8));
  const line = out.split('\n').find(l => l.startsWith('  [0.6, 0.8]'));
  assert.ok(line, 'bucket row must be present');
  assert.ok(!line.includes('thin sample'), `n=8 must be rendered: "${line}"`);
  assert.ok(line.includes('0.500'), `n=8 must show a numeric match rate: "${line}"`);
});

test('MIN_BUCKET_N=8: a bucket with 9 observations is rendered', () => {
  const out = _renderProposalCalibration(calWithSingleBucketCount(9));
  const line = out.split('\n').find(l => l.startsWith('  [0.6, 0.8]'));
  assert.ok(line, 'bucket row must be present');
  assert.ok(!line.includes('thin sample'), `n=9 must be rendered: "${line}"`);
});

test('MIN_BUCKET_N=8: a bucket with 7 observations is thin', () => {
  const out = _renderProposalCalibration(calWithSingleBucketCount(7));
  const line = out.split('\n').find(l => l.startsWith('  [0.6, 0.8]'));
  assert.ok(line, 'bucket row must be present');
  assert.ok(line.includes('thin sample'), `n=7 must be thin: "${line}"`);
});

test('CALIBRATION_MIN_SAMPLES (doctor.py global floor) is no longer declared or used as the per-bucket gate', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');
  assert.ok(!src.includes('const CALIBRATION_MIN_SAMPLES'),
    'the doctor global-total constant must no longer be declared in this file ' +
    '(a comment MAY still name it, explaining why it was replaced)');
  assert.ok(!src.includes('n < CALIBRATION_MIN_SAMPLES'),
    'the per-bucket gate must no longer read the doctor global-total constant');
  assert.ok(src.includes('const MIN_BUCKET_N'),
    'the per-bucket mastermind_calibration.py-mirrored constant must be declared');
  assert.ok(src.includes('n < MIN_BUCKET_N'),
    'the per-bucket gate must read MIN_BUCKET_N');
});

// D6 fix1 item 2 (IMPORTANT): loader failures must be visible.
test('_loadProposalCalibration fails open AND logs exactly one console.warn per failure branch', (t) => {
  const cases = [
    {
      label: 'spawn call itself throws',
      spawn: () => { throw new Error('spawn ENOENT'); },
    },
    {
      label: 'real spawnSync ENOENT shape (res.error set, no throw)',
      spawn: () => ({
        error: Object.assign(new Error('spawnSync python3 ENOENT'), { code: 'ENOENT' }),
        status: null, signal: null, stdout: undefined, stderr: undefined,
      }),
    },
    {
      label: 'real spawnSync timeout shape (res.error + signal, status null)',
      spawn: () => ({
        error: Object.assign(new Error('spawnSync python3 ETIMEDOUT'), { code: 'ETIMEDOUT' }),
        status: null, signal: 'SIGTERM', stdout: '', stderr: '',
      }),
    },
    {
      label: 'non-zero exit status',
      spawn: () => ({ status: 1, stdout: '', stderr: 'Traceback (most recent call last): boom' }),
    },
    {
      label: 'signal/timeout with status===null, no res.error field',
      spawn: () => ({ status: null, signal: 'SIGTERM', stdout: '', stderr: '' }),
    },
    {
      label: 'empty stdout',
      spawn: () => ({ status: 0, stdout: '', stderr: '' }),
    },
    {
      label: 'unparseable stdout',
      spawn: () => ({ status: 0, stdout: 'not json', stderr: '' }),
    },
    {
      label: 'JSON without a buckets array',
      spawn: () => ({ status: 0, stdout: JSON.stringify({ buckets: 'x' }), stderr: '' }),
    },
  ];

  for (const { label, spawn } of cases) {
    const warnSpy = t.mock.method(console, 'warn', () => {});
    const result = _loadProposalCalibration({ spawn });
    assert.equal(result, null, `${label}: must return null`);
    assert.equal(warnSpy.mock.calls.length, 1, `${label}: must log exactly one console.warn`);
    const [msg] = warnSpy.mock.calls[0].arguments;
    assert.ok(msg.startsWith('[review] calibration addendum unavailable: '),
      `${label}: warn text must use the new prefix — got "${msg}"`);
    assert.match(msg, /status=/, `${label}: warn must name the status`);
    assert.match(msg, /signal=/, `${label}: warn must name the signal`);
    assert.match(msg, /stderr="/, `${label}: warn must include a stderr tail`);
    warnSpy.mock.restore();
  }
});

test('a stderr tail longer than 200 chars is truncated to its last 200 chars', (t) => {
  const longStderr = 'x'.repeat(50) + 'y'.repeat(250); // 300 chars total
  const warnSpy = t.mock.method(console, 'warn', () => {});
  const result = _loadProposalCalibration({
    spawn: () => ({ status: 1, stdout: '', stderr: longStderr }),
  });
  assert.equal(result, null);
  assert.equal(warnSpy.mock.calls.length, 1);
  const [msg] = warnSpy.mock.calls[0].arguments;
  const tailMatch = msg.match(/stderr="([^"]*)"$/);
  assert.ok(tailMatch, 'warn must end with a quoted stderr tail');
  assert.equal(tailMatch[1].length, 200, 'the stderr tail must be capped at 200 chars');
  assert.equal(tailMatch[1], longStderr.slice(-200));
});

test('_loadProposalCalibration parses a well-formed report from an injected spawn, with no warning', (t) => {
  const warnSpy = t.mock.method(console, 'warn', () => {});
  const spawn = () => ({ status: 0, stdout: JSON.stringify(CAL), stderr: '' });
  const result = _loadProposalCalibration({ spawn });
  assert.deepEqual(result, CAL);
  assert.equal(warnSpy.mock.calls.length, 0, 'the happy path must not warn');
});

test('fail-open proof: buildStrategyPrompt with a failing injected loader equals the no-calibration prompt', (t) => {
  const warnSpy = t.mock.method(console, 'warn', () => {});
  const failingSpawn = () => { throw new Error('spawn ENOENT'); };
  const cal = _loadProposalCalibration({ spawn: failingSpawn });
  assert.equal(cal, null, 'loader must fail open to null');
  const withFailedLoader = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS, cal);
  const legacy = buildStrategyPrompt(STRATEGY, TRADE_PACK, COUNTERFACTUALS);
  assert.equal(withFailedLoader, legacy,
    'a failed calibration load must render a prompt byte-identical to the no-calibration prompt');
  assert.equal(warnSpy.mock.calls.length, 1, 'the failure must still be logged exactly once');
});

// D6 fix1 item 4 (RULED): run() calls the loader ONCE, before the loop, and
// threads the same calibration object through every _reviewOne call.
test('run() loads calibration exactly once and threads the SAME object into every _reviewOne call', async () => {
  let loadCount = 0;
  const fakeCal = { buckets: [], marker: 'fake-calibration' };
  const loadCalibration = () => { loadCount += 1; return fakeCal; };
  const strategies = [{ id: 'S1' }, { id: 'S2' }, { id: 'S3' }];
  const fetchStrategies = async () => strategies;
  const seen = [];
  const reviewOne = async (strategy, opts) => {
    seen.push({ strategyId: strategy.id, opts });
    return { strategy_id: strategy.id };
  };

  const result = await run({
    notify: () => {},
    fetchStrategies,
    reviewOne,
    loadCalibration,
  });

  assert.equal(loadCount, 1, 'the loader must be called exactly once for the whole run, not once per strategy');
  assert.equal(seen.length, 3, 'every strategy must still be reviewed');
  for (const call of seen) {
    assert.equal(call.opts.calibration, fakeCal,
      `_reviewOne for ${call.strategyId} must receive the SAME calibration object reference`);
    assert.equal(call.opts.dryRun, false);
  }
  assert.equal(result.strategiesReviewed, 0); // the fake reviewOne never returns memo_id
});

test('_reviewOne no longer calls _loadProposalCalibration itself — it is hoisted into run()', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');
  const start = src.indexOf('async function _reviewOne(');
  const end = src.indexOf('\nasync function run(', start);
  assert.ok(start !== -1 && end !== -1 && end > start,
    'could not locate the _reviewOne...run() source region — brief item 4 assumes this layout');
  const body = src.slice(start, end);
  assert.ok(!body.includes('_loadProposalCalibration()'),
    '_reviewOne must not call the loader itself — run() loads it once and threads it through');
  assert.ok(body.includes('calibration'),
    '_reviewOne must still consume a `calibration` value from its options object');
});

test('the 2026-05-19 removal note no longer claims the addenda logic is gone', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');
  assert.ok(!src.includes('Phase 2F calibration-addenda prepend logic removed'),
    'the stale removal note must be replaced by the restored block');
  assert.ok(src.includes('_renderProposalCalibration'));
});
