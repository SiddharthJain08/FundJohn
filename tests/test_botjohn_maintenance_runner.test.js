'use strict';

/**
 * tests/test_botjohn_maintenance_runner.test.js
 *
 * Verifies the BotJohn 12:00 ET daily-maintenance wrapper at
 * src/agent/run_maintenance.js. Tests inject stubs for runClaudeBin /
 * getWebhook / postWebhook so no real claude-bin runs in CI.
 *
 * Run:
 *   node --test tests/test_botjohn_maintenance_runner.test.js
 */

process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';

const { test } = require('node:test');
const assert    = require('node:assert/strict');

const runner = require('../src/agent/run_maintenance');

// ── formatReport ──────────────────────────────────────────────────────

test('formatReport keeps green-light bodies verbatim under footer', () => {
  const body = '✅ **Daily maintenance — 2026-04-30**\nPipeline ran cleanly. Doctor: pass (11/11).';
  const out = runner.formatReport(body, 0.42, 187_000);
  assert.ok(out.startsWith(body), 'short body must be preserved verbatim');
  assert.ok(out.includes('_session cost: $0.42 | duration: 187s_'), 'footer must include cost + duration');
  assert.ok(!out.includes('exceeded'), 'no cost-cap warning under budget');
  assert.ok(out.length <= 1900, `output ≤1900 chars, got ${out.length}`);
});

test('formatReport slices long inputs and stays ≤1900 chars total', () => {
  const long = 'X'.repeat(5000);
  const out = runner.formatReport(long, 0.10, 60_000);
  assert.ok(out.length <= 1900, `output ≤1900 chars, got ${out.length}`);
  assert.ok(out.includes('_session cost: $0.10'), 'footer present after slice');
});

test('formatReport appends cost-cap warning when costUsd > $5', () => {
  const out = runner.formatReport('🔧 long maintenance', 7.31, 1_200_000);
  assert.ok(out.includes('_session cost: $7.31'));
  assert.ok(/⚠️ cost exceeded \$\d+\.\d{2} budget/.test(out),
    `should include cost-cap warning, got: ${out}`);
});

test('formatReport handles missing/NaN cost gracefully', () => {
  const out = runner.formatReport('hello', NaN, 1000);
  assert.ok(out.includes('_session cost: $0.00 | duration: 1s_'),
    `should fall back to $0.00, got: ${out}`);
});

// ── clipToTemplate (preamble stripping) ───────────────────────────────

test('clipToTemplate strips claude-bin narration before ✅ marker', () => {
  // Real fixture from the May 1 maintenance run that prompted this fix.
  const fixture = [
    'All data gathered. Let me compose the maintenance report.',
    '',
    '**Summary:**',
    '- Pipeline: all 8/8 steps completed cleanly at 14:17 ET',
    '',
    '---',
    '',
    '✅ **Daily maintenance — 2026-05-01**',
    'Pipeline ran cleanly. Doctor: 10/12 pass.',
    'No action taken.',
  ].join('\n');
  const out = runner.clipToTemplate(fixture);
  assert.ok(out.startsWith('✅ **Daily maintenance'),
    `clipped output must begin with the template emoji, got: ${JSON.stringify(out.slice(0,80))}`);
  assert.ok(!out.includes('All data gathered'),
    'preamble narration must be removed');
});

test('clipToTemplate handles 🔧 fix-and-recovered marker', () => {
  const fixture = 'Here is what I found... \n---\n🔧 **Daily maintenance — 2026-05-04**\nDetected: ...';
  const out = runner.clipToTemplate(fixture);
  assert.ok(out.startsWith('🔧 **Daily maintenance'));
});

test('clipToTemplate handles 🚨 escalation marker', () => {
  const fixture = 'Investigation:\n\n🚨 **Daily maintenance — 2026-05-05**\nCould not auto-fix...';
  const out = runner.clipToTemplate(fixture);
  assert.ok(out.startsWith('🚨 **Daily maintenance'));
});

test('clipToTemplate falls back to original text when no marker found', () => {
  // Defensive: never drop the message — the user still needs SOMETHING
  // even if the LLM produced a fully-malformed response.
  const fixture = 'Pipeline looks healthy but I forgot to use the template';
  assert.equal(runner.clipToTemplate(fixture), fixture);
});

test('clipToTemplate is a no-op when text already starts with marker', () => {
  const fixture = '✅ **Daily maintenance — 2026-05-02**\nPipeline ran cleanly.';
  assert.equal(runner.clipToTemplate(fixture), fixture);
});

test('formatReport integrates clipping: real-world preamble dropped', () => {
  const fixture = [
    'All checks complete. Pipeline log confirms all 8 steps finished.',
    '',
    '---',
    '',
    '✅ **Daily maintenance — 2026-04-30**',
    'Pipeline ran cleanly.',
    'No action taken.',
  ].join('\n');
  const out = runner.formatReport(fixture, 0.18, 78_000);
  assert.ok(out.startsWith('✅ **Daily maintenance'),
    'Discord-bound output must lead with the template emoji');
  assert.ok(out.includes('_session cost: $0.18 | duration: 78s_'));
});

// ── main(): dependency-injected end-to-end ────────────────────────────

function _stubFor({ runClaudeBin, getWebhook, postWebhook } = {}) {
  const calls = { post: [], webhook: [] };
  return {
    calls,
    deps: {
      runClaudeBin: runClaudeBin || (async () => ({ result: '✅ green', costUsd: 0.05, durationMs: 12_000, raw: '{}' })),
      getWebhook:   getWebhook   || (async () => { calls.webhook.push(['botjohn','general']); return 'https://discord.com/api/webhooks/x/y'; }),
      postWebhook:  postWebhook  || (async (url, content) => { calls.post.push({ url, content }); return { ok: true, status: 204, body: '' }; }),
    },
  };
}

test('main(): green-light path posts the report once and exits 0', async () => {
  const { deps, calls } = _stubFor({});
  const before = process.exitCode;
  process.exitCode = 0;
  const r = await runner.main(deps);
  assert.equal(r.ok, true);
  assert.equal(calls.post.length, 1, 'should post exactly once');
  assert.ok(calls.post[0].content.includes('✅ green'), 'content must include the assistant body');
  assert.ok(calls.post[0].content.includes('_session cost:'), 'content must include footer');
  assert.equal(process.exitCode, 0);
  process.exitCode = before;
});

test('main(): claude-bin throw → fallback alert posted, exit 1', async () => {
  const { deps, calls } = _stubFor({
    runClaudeBin: async () => { throw new Error('claude-bin exit 137: SIGKILL'); },
  });
  process.exitCode = 0;
  const r = await runner.main(deps);
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'wrapper_failure');
  assert.equal(calls.post.length, 1, 'fallback must still post');
  assert.ok(calls.post[0].content.startsWith('🚨 BotJohn maintenance run failed:'),
    `expected 🚨 prefix, got: ${calls.post[0].content}`);
  assert.ok(calls.post[0].content.includes('exit 137'), 'must surface underlying error');
  assert.equal(process.exitCode, 1);
});

test('main(): webhook lookup returns null → exit 1, no post attempted', async () => {
  const { deps, calls } = _stubFor({
    getWebhook: async () => null,
  });
  process.exitCode = 0;
  const r = await runner.main(deps);
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'no_webhook');
  assert.equal(calls.post.length, 0, 'must not post when webhook is missing');
  assert.equal(process.exitCode, 1);
});

test('main(): webhook 5xx → exit 1, error reason returned', async () => {
  const { deps } = _stubFor({
    postWebhook: async () => ({ ok: false, status: 503, body: 'service unavailable' }),
  });
  process.exitCode = 0;
  const r = await runner.main(deps);
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'post_failed');
  assert.equal(r.status, 503);
  assert.equal(process.exitCode, 1);
});

// ── prompt + buildPrompt ──────────────────────────────────────────────

test('buildPrompt: interpolates {{TODAY_ISO}} everywhere', () => {
  const out = runner.buildPrompt({ today: '2026-04-30' });
  assert.ok(!out.includes('{{TODAY_ISO}}'), 'no template tokens left over');
  assert.ok(out.includes('2026-04-30'), 'date must appear in body');
  // Sanity: the prompt mentions the actual scripts BotJohn will run
  assert.ok(out.includes('python3 src/maintenance/doctor.py --json'));
  assert.ok(out.includes('node src/pipeline/daily_health_digest.js --dry-run'));
  assert.ok(out.includes('python3 scripts/run_pipeline.py --force-resume'));
});

// Earlier failure-path tests intentionally set process.exitCode=1 to
// prove the wrapper signals failure to systemd. Reset here so node:test
// doesn't tag the whole file as failed when the individual subtests pass.
test('cleanup: reset process.exitCode for runner harness', () => {
  process.exitCode = 0;
});
