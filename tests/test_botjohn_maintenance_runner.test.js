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
  assert.ok(calls.post[0].content.startsWith('🚨 BotJohn maintenance run failed'),
    `expected 🚨 prefix, got: ${calls.post[0].content}`);
  assert.ok(calls.post[0].content.includes('mode='),
    'fallback must include mode= so journal alerts identify which timer fired');
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

test('buildPrompt: interpolates {{TODAY_ISO}} everywhere (default daily mode)', () => {
  const out = runner.buildPrompt({ today: '2026-04-30' });
  assert.ok(!out.includes('{{TODAY_ISO}}'), 'no template tokens left over');
  assert.ok(out.includes('2026-04-30'), 'date must appear in body');
  // Sanity: the prompt mentions the actual scripts BotJohn will run
  assert.ok(out.includes('python3 src/maintenance/doctor.py --json'));
  assert.ok(out.includes('node src/pipeline/daily_health_digest.js --dry-run'));
  assert.ok(out.includes('python3 scripts/run_pipeline.py --force-resume'));
  // Daily prompt must NOT contain Saturday-specific scripts (regression guard
  // against accidental cross-pollination of templates).
  assert.ok(!out.includes('saturday_runs'),
    'daily mode must not reference saturday_runs');
  assert.ok(!out.includes('saturday_brain_finisher'),
    'daily mode must not reference saturday_brain_finisher');
});

test('buildPrompt: mode=saturday selects research-maintenance template', () => {
  const out = runner.buildPrompt({ today: '2026-05-02', mode: 'saturday' });
  assert.ok(!out.includes('{{TODAY_ISO}}'), 'no template tokens left over');
  assert.ok(out.includes('2026-05-02'));
  // Saturday-specific scripts and tables
  assert.ok(out.includes('saturday_runs'),
    'saturday mode must query saturday_runs');
  assert.ok(out.includes('saturday_brain_finisher.js'),
    'saturday mode must reference the surgical Phase-6 lever');
  assert.ok(out.includes('saturday_brain_retry_failed.js'),
    'saturday mode must reference the fetch-failed retry lever');
  assert.ok(out.includes('systemctl start openclaw-saturday-brain.service'),
    'saturday mode must reference the full re-trigger lever');
  assert.ok(out.includes('curated_candidates'),
    'saturday mode must query bucket distribution');
  assert.ok(out.includes('paper_gate_decisions'),
    'saturday mode must query paperhunter outcomes');
  // Must not leak weekday-only commands
  assert.ok(!out.includes('--force-resume'),
    'saturday mode must not reference --force-resume (saturday-brain has no resume flag)');
});

test('buildPrompt: mode=saturday-verify is read-only and reads yesterday', () => {
  const out = runner.buildPrompt({ today: '2026-05-03', mode: 'saturday-verify' });
  assert.ok(!out.includes('{{TODAY_ISO}}'));
  assert.ok(out.includes('2026-05-03'));
  assert.ok(out.includes('READ-ONLY'),
    'verify prompt must explicitly mark itself read-only');
  assert.ok(out.includes("INTERVAL '1 day'"),
    'verify prompt must read yesterday, not today');
  // Verify must NOT contain mutation commands
  assert.ok(!out.includes('saturday_brain_finisher.js'),
    'verify must not reference recovery scripts (read-only)');
  assert.ok(!out.includes('systemctl start openclaw-saturday-brain.service'),
    'verify must not reference re-triggers (read-only)');
});

test('buildPrompt: unknown mode throws with helpful message', () => {
  assert.throws(
    () => runner.buildPrompt({ today: '2026-05-02', mode: 'unknown-mode-xyz' }),
    /unknown mode: unknown-mode-xyz/,
  );
});

// ── per-mode caps + prompt templating ─────────────────────────────────
//
// Pre-2026-05-03 the cost cap and timeout were single global values, and
// the prompts themselves had hardcoded "$5" / "30 min" boundaries. That
// meant raising the cap via env var didn't actually let BotJohn spend
// more — the prompt still told him to stop at $5. These tests guard the
// per-mode resolution AND the prompt-side interpolation so the boundary
// BotJohn reads matches the cap actually in effect.

test('costCapFor + timeoutMsFor: mode-specific defaults', () => {
  assert.equal(runner.costCapFor('daily'),            5.00);
  assert.equal(runner.costCapFor('saturday'),         15.00);
  assert.equal(runner.costCapFor('saturday-verify'),  3.00);
  assert.equal(runner.timeoutMsFor('daily'),           1_800_000);
  assert.equal(runner.timeoutMsFor('saturday'),        3_000_000);
  assert.equal(runner.timeoutMsFor('saturday-verify'),   900_000);
});

test('costCapFor: unknown mode falls back to daily', () => {
  // We don't want a typo in --mode to silently get 0 cap.
  assert.equal(runner.costCapFor('totally-bogus'), runner.costCapFor('daily'));
  assert.equal(runner.timeoutMsFor('totally-bogus'), runner.timeoutMsFor('daily'));
});

test('buildPrompt: interpolates {{COST_CAP_USD}} per mode (daily=$5, saturday=$15, verify=$3)', () => {
  const daily   = runner.buildPrompt({ today: '2026-05-04', mode: 'daily' });
  const sat     = runner.buildPrompt({ today: '2026-05-09', mode: 'saturday' });
  const verify  = runner.buildPrompt({ today: '2026-05-10', mode: 'saturday-verify' });

  // Daily prompt's boundary line should mention $5.00, not $15 or $3.
  assert.ok(daily.includes('budget under $5.00'),
    `daily prompt must say budget under $5.00, got fragments: ${
      daily.split('\n').filter(l => l.includes('budget')).map(l => l.trim()).join(' | ')}`);

  // Saturday prompt's boundary line must say $15.00 — and the
  // ~$40 vs ~$5 fragment must NOT leak (that was the pre-fix copy).
  assert.ok(sat.includes('audit-session budget under $15.00'),
    `saturday prompt must say audit budget under $15.00`);
  assert.ok(!sat.includes('vs ~$5.'),
    `saturday prompt must not retain the legacy '~$5' boundary copy`);

  // Verify prompt mentions the lower cap.
  assert.ok(verify.includes('audit budget under $3.00'),
    `saturday-verify prompt must say audit budget under $3.00`);
});

test('buildPrompt: interpolates {{TIMEOUT_MIN}} per mode', () => {
  const sat = runner.buildPrompt({ today: '2026-05-09', mode: 'saturday' });
  assert.ok(sat.includes('have 50 minutes wrapper budget'),
    `saturday prompt must reflect the 50-min timeout (not the legacy 30 min)`);
  assert.ok(sat.includes('Wrapper times out at 50 min.'),
    `saturday prompt's recovery boundary must say Wrapper times out at 50 min`);
});

test('formatReport: cost-cap warning uses caller-supplied cap', () => {
  // Saturday spend $12, cap $15 → no warning
  const sat = runner.formatReport('🔧 saturday work', 12.00, 1_500_000, 15.00);
  assert.ok(!sat.includes('exceeded'),
    `$12 of a $15 cap should NOT trigger warning`);

  // Same spend $12, daily cap $5 → must warn
  const daily = runner.formatReport('🔧 daily fix', 12.00, 1_500_000, 5.00);
  assert.ok(/⚠️ cost exceeded \$5\.00 budget/.test(daily),
    `$12 of a $5 cap should warn with the caller-supplied cap`);

  // Verify cap $3, spend $3.50 → must warn
  const verify = runner.formatReport('✅ verify', 3.50, 200_000, 3.00);
  assert.ok(/⚠️ cost exceeded \$3\.00 budget/.test(verify),
    `verify cap should be $3.00 in the warning text`);
});

test('main: per-mode cap propagates through to runClaudeBin timeout + footer', async () => {
  let capturedTimeout = null;
  const stub = _stubFor({
    runClaudeBin: async (prompt, opts) => {
      capturedTimeout = opts?.timeoutMs;
      return { result: '🔧 **Saturday research — 2026-05-09**\nfix applied', costUsd: 6.50, durationMs: 600_000, raw: '{}' };
    },
  });
  process.exitCode = 0;
  const r = await runner.main({ ...stub.deps, mode: 'saturday' });
  assert.equal(r.ok, true);
  assert.equal(capturedTimeout, 3_000_000,
    `saturday mode should pass 50min timeout into runClaudeBin`);
  // $6.50 < $15 saturday cap → no warning even though it exceeds the daily cap
  assert.ok(!stub.calls.post[0].content.includes('exceeded'),
    `saturday cost $6.50 under $15 cap should NOT warn`);
});

// ── clipToTemplate handles all three modes' header markers ────────────

test('clipToTemplate strips preamble before ✅ Saturday research', () => {
  const fixture = 'Investigation:\n- saturday_runs row...\n\n✅ **Saturday research — 2026-05-02**\nbody';
  const out = runner.clipToTemplate(fixture);
  assert.ok(out.startsWith('✅ **Saturday research'),
    `Saturday research green-light must clip preamble, got: ${out.slice(0,60)}`);
});

test('clipToTemplate strips preamble before 🔧 Saturday research', () => {
  const fixture = 'I found a Phase-6 bug. Here is what I did.\n\n🔧 **Saturday research — 2026-05-02**\nDetected: ...';
  const out = runner.clipToTemplate(fixture);
  assert.ok(out.startsWith('🔧 **Saturday research'));
});

test('clipToTemplate strips preamble before ✅ Saturday verify', () => {
  const fixture = 'Yesterday completed cleanly.\n\n✅ **Saturday verify — 2026-05-03**\nbody';
  const out = runner.clipToTemplate(fixture);
  assert.ok(out.startsWith('✅ **Saturday verify'));
});

// ── main(): mode dispatch wires through to runClaudeBin prompt ─────────

test('main(): mode passed through deps overrides argv', async () => {
  let capturedPrompt = null;
  const stub = _stubFor({
    runClaudeBin: async (prompt) => {
      capturedPrompt = prompt;
      return { result: '✅ **Saturday research — 2026-05-02**\nbody', costUsd: 0.10, durationMs: 5_000, raw: '{}' };
    },
  });
  process.exitCode = 0;
  const r = await runner.main({ ...stub.deps, mode: 'saturday' });
  assert.equal(r.ok, true);
  assert.equal(r.mode, 'saturday');
  assert.ok(capturedPrompt.includes('saturday_runs'),
    'main() must pass the saturday prompt to runClaudeBin when mode=saturday');
  assert.ok(stub.calls.post[0].content.startsWith('✅ **Saturday research'),
    'posted message must lead with Saturday template (clipped)');
});

test('main(): unknown mode posts no report and exits 1', async () => {
  const stub = _stubFor({});
  process.exitCode = 0;
  const r = await runner.main({ ...stub.deps, mode: 'totally-bogus' });
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'unknown_mode');
  assert.equal(stub.calls.post.length, 0, 'no post when mode is unrecognized');
  assert.equal(process.exitCode, 1);
});

test('main(): claude-bin auth-error JSON triggers fallback (regression for 2026-05-02)', async () => {
  // claude-bin can return success-shaped JSON when OAuth is expired —
  // result is the literal "Failed to authenticate..." string and cost=0.
  // Wrapper must detect this and post 🚨 fallback, not the error string.
  // The auth-failure detection lives inside runClaudeBin, so we exercise
  // it by stubbing runClaudeBin to throw the same way auth detection
  // would (since main() consumes runClaudeBin's promise rejection).
  const { deps, calls } = _stubFor({
    runClaudeBin: async () => {
      throw new Error('claude-bin auth failure: Failed to authenticate. API Error: 401 ...');
    },
  });
  process.exitCode = 0;
  const r = await runner.main(deps);
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'wrapper_failure');
  assert.equal(calls.post.length, 1, 'auth failure must trigger fallback post');
  assert.ok(calls.post[0].content.startsWith('🚨'),
    'fallback post must start with 🚨');
  assert.ok(calls.post[0].content.toLowerCase().includes('auth'),
    'fallback message should surface "auth" so the alert is actionable');
  assert.equal(process.exitCode, 1);
});

test('main(): no mode → defaults to daily (back-compat with weekday timer)', async () => {
  let capturedPrompt = null;
  const stub = _stubFor({
    runClaudeBin: async (prompt) => {
      capturedPrompt = prompt;
      return { result: '✅ **Daily maintenance — 2026-05-04**\nclean', costUsd: 0.05, durationMs: 4_000, raw: '{}' };
    },
  });
  process.exitCode = 0;
  // Note: no `mode` in deps; relies on default. argv may or may not have
  // --mode (depends on how tests are invoked). buildPrompt's default
  // argument is 'daily'.
  const argvBefore = process.argv;
  process.argv = ['node', 'run_maintenance.js']; // ensure no --mode flag
  try {
    const r = await runner.main(stub.deps);
    assert.equal(r.ok, true);
    assert.equal(r.mode, 'daily');
    assert.ok(capturedPrompt.includes('--force-resume'),
      'no-flag invocation must select daily prompt');
    assert.ok(!capturedPrompt.includes('saturday_runs'),
      'no-flag invocation must NOT select saturday prompt');
  } finally {
    process.argv = argvBefore;
  }
});

// Earlier failure-path tests intentionally set process.exitCode=1 to
// prove the wrapper signals failure to systemd. Reset here so node:test
// doesn't tag the whole file as failed when the individual subtests pass.
test('cleanup: reset process.exitCode for runner harness', () => {
  process.exitCode = 0;
});
