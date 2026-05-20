'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');
const mod  = require(path.join(ROOT, 'src/agent/curators/synthesizer.js'));

const MEMO = {
  id: 7,
  strategy_id: 'S9_dual_momentum',
  memo_date: '2026-05-20',
  markdown_body: '## Recommendation\nSize 3.0% NAV.',
  recommendations: { recommended_size_pct: 0.030 },
};

const CRITIQUES = [
  { critic_role: 'aggressive',   critique_text: 'too timid',     cited_metrics: { proposed_size_pct_delta: +0.005 } },
  { critic_role: 'conservative', critique_text: 'too aggressive', cited_metrics: { proposed_size_pct_delta: -0.006 } },
  { critic_role: 'neutral',      critique_text: 'no issues found', cited_metrics: { no_issues_found: true } },
];

test('synthesize returns adjusted recommendation parsed from JSON', async () => {
  const fakeRunner = async (_prompt) => JSON.stringify({
    strategy_id: 'S9_dual_momentum',
    original_recommended_size_pct: 0.030,
    adjusted_recommended_size_pct: 0.024,
    adjustment_reason: 'Conservative accepted',
    critics_accepted: ['conservative'],
    critics_rejected: [
      { critic: 'aggressive', reason: 'cherry-picked winners' },
      { critic: 'neutral', reason: 'no issues' },
    ],
  });
  let persisted = null;
  mod._setRunnerForTests(fakeRunner);
  mod._setWriterForTests(async (row) => { persisted = row; });

  const result = await mod.synthesize(MEMO, CRITIQUES, [], [], 0.030, { weekOf: '2026-05-20' });

  assert.equal(result.adjusted_recommended_size_pct, 0.024);
  assert.deepEqual(result.critics_accepted, ['conservative']);
  assert.equal(persisted.adjusted_recommended_size_pct, 0.024);
  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('synthesize falls back to original when no critiques given', async () => {
  // Empty critiques (e.g. all 3 critics failed)
  let runnerCalled = false;
  mod._setRunnerForTests(async () => { runnerCalled = true; return ''; });
  let persisted = null;
  mod._setWriterForTests(async (row) => { persisted = row; });

  const result = await mod.synthesize(MEMO, [], [], [], 0.030, { weekOf: '2026-05-20' });

  assert.equal(runnerCalled, false, 'runner not invoked when no critiques');
  assert.equal(result.adjusted_recommended_size_pct, 0.030);
  assert.equal(persisted.adjustment_reason, 'ALL_CRITICS_FAILED, defaulted to original');

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('synthesize falls back when LLM call throws', async () => {
  mod._setRunnerForTests(async () => { throw new Error('Opus down'); });
  let persisted = null;
  mod._setWriterForTests(async (row) => { persisted = row; });

  const result = await mod.synthesize(MEMO, CRITIQUES, [], [], 0.030, { weekOf: '2026-05-20' });

  assert.equal(result.adjusted_recommended_size_pct, 0.030);
  assert.match(persisted.adjustment_reason, /SYNTHESIZER_FAILED/);

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});

test('synthesize falls back when LLM returns unparseable output', async () => {
  mod._setRunnerForTests(async () => 'not json at all');
  let persisted = null;
  mod._setWriterForTests(async (row) => { persisted = row; });

  const result = await mod.synthesize(MEMO, CRITIQUES, [], [], 0.030, { weekOf: '2026-05-20' });

  assert.equal(result.adjusted_recommended_size_pct, 0.030);
  assert.match(persisted.adjustment_reason, /SYNTHESIZER_PARSE_FAILED/);

  mod._setRunnerForTests(null);
  mod._setWriterForTests(null);
});
