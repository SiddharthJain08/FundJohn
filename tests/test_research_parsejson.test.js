'use strict';

/**
 * tests/test_research_parsejson.test.js
 *
 * Guards `ResearchOrchestrator._parseJSON` against the failure mode that
 * dropped saturday-brain to 0/2 implementations on 2026-05-02:
 *
 *   - Pre-fix: greedy regex /[\[{][\s\S]*[\]}]/ matched across markdown,
 *     preamble, and multiple JSON blocks → JSON.parse threw → null →
 *     paperhunter return value `{rejection_reason_if_any: "parse_failed"}` →
 *     0 candidates tiered → 0 coded → 0 staged.
 *
 * Post-fix priority:
 *   1. Fenced ```json``` block (Sonnet 4.6's actual default behavior).
 *   2. Balanced-brace extraction from first `{`/`[`.
 *   3. Direct JSON.parse of trimmed text.
 *   4. Log a truncated snippet on total failure.
 *
 * Run:
 *   node --test tests/test_research_parsejson.test.js
 */

process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';

const { test } = require('node:test');
const assert    = require('node:assert/strict');

const ResearchOrchestrator = require('../src/agent/research/research-orchestrator');

// _parseJSON is an instance method; instantiating doesn't connect to DB
// because the constructor is lazy on its DB pool.
const orch = new ResearchOrchestrator();

test('_parseJSON: returns null on empty input', () => {
  assert.equal(orch._parseJSON(null), null);
  assert.equal(orch._parseJSON(''), null);
  assert.equal(orch._parseJSON(undefined), null);
});

test('_parseJSON: passes through objects unchanged', () => {
  const obj = { a: 1, b: [2, 3] };
  assert.deepEqual(orch._parseJSON(obj), obj);
});

test('_parseJSON: bare JSON object (the prompt-compliant happy path)', () => {
  const raw = '{"strategy_id": "S_test", "rejection_reason_if_any": null}';
  assert.deepEqual(orch._parseJSON(raw), { strategy_id: 'S_test', rejection_reason_if_any: null });
});

test('_parseJSON: fenced ```json block strips wrapper (the actual 2026-05-02 case)', () => {
  // Real Sonnet 4.6 paperhunter output shape — "Return a single raw JSON
  // object. No markdown" is in the prompt and is regularly ignored.
  const raw = [
    'Here is my analysis:',
    '',
    '```json',
    '{"strategy_id": "S_test", "rejection_reason_if_any": null, "data_requirements": {"required": ["prices"]}}',
    '```',
    '',
    'Done.',
  ].join('\n');
  const out = orch._parseJSON(raw);
  assert.equal(out.strategy_id, 'S_test');
  assert.equal(out.rejection_reason_if_any, null);
  assert.deepEqual(out.data_requirements.required, ['prices']);
});

test('_parseJSON: handles ``` fence without "json" language tag', () => {
  const raw = '```\n{"foo": "bar"}\n```';
  assert.deepEqual(orch._parseJSON(raw), { foo: 'bar' });
});

test('_parseJSON: balanced-brace extraction from preamble + JSON', () => {
  // No fence, but JSON embedded after prose — balanced-brace scan must
  // match { ... } correctly even with nested objects.
  const raw = 'Analysis complete. Output:\n{"a": 1, "b": {"nested": true}, "c": [1,2,3]}\nThanks';
  assert.deepEqual(orch._parseJSON(raw), { a: 1, b: { nested: true }, c: [1, 2, 3] });
});

test('_parseJSON: ignores trailing junk after closing brace', () => {
  // The pre-fix greedy regex would match through to the trailing `}` in
  // unrelated prose. Balanced-brace extraction stops at the first valid
  // matching close, leaving the trailing junk untouched.
  const raw = '{"valid": true} extra prose here {with} more {braces}';
  const out = orch._parseJSON(raw);
  assert.equal(out.valid, true);
});

test('_parseJSON: handles arrays', () => {
  const raw = 'list:\n[1, 2, 3]';
  assert.deepEqual(orch._parseJSON(raw), [1, 2, 3]);
});

test('_parseJSON: tolerates braces inside string values', () => {
  // The balanced-brace scanner must skip braces inside JSON string literals.
  const raw = '{"formula": "x[i] = {a, b, c}", "n": 1}';
  const out = orch._parseJSON(raw);
  assert.equal(out.formula, 'x[i] = {a, b, c}');
  assert.equal(out.n, 1);
});

test('_parseJSON: returns null + logs on totally invalid input', () => {
  const captured = [];
  const orig = console.error;
  console.error = (...args) => captured.push(args.join(' '));
  try {
    const out = orch._parseJSON('this is not json at all, no braces, just prose');
    assert.equal(out, null);
    // No braces → no warning to log (warning fires only if we attempted
    // to parse and failed, not for "no JSON shape detected").
  } finally {
    console.error = orig;
  }
});

test('_parseJSON: returns null + logs raw head when JSON is malformed', () => {
  const captured = [];
  const orig = console.error;
  console.error = (...args) => captured.push(args.join(' '));
  try {
    // Has braces, but JSON is broken (missing closing brace).
    const out = orch._parseJSON('{"a": 1, "b": 2');
    assert.equal(out, null);
    assert.ok(captured.some((c) => c.includes('_parseJSON failed')),
      'should log a diagnostic line so future operators can see what came back');
  } finally {
    console.error = orig;
  }
});

test('_parseJSON: regression — would have caught 2026-05-02 case', () => {
  // Reproduce the actual claude-bin output shape that hit parse_failed:
  // assistant text includes a paragraph + fenced JSON.
  const realFixture = [
    'I analyzed the paper at https://alphaarchitect.com/rethinking-trend-following.',
    '',
    'After running through the gates, here is the result:',
    '',
    '```json',
    '{',
    '  "candidate_id": "f0435bcd-5ea1-46a9-a972-b57651fd9108",',
    '  "strategy_id": "S_regime_dependent_trend_following",',
    '  "source_url": "https://alphaarchitect.com/rethinking-trend-following-optimal-regime-dependent-allocation/",',
    '  "rejection_reason_if_any": null,',
    '  "data_requirements": {"required": ["prices"], "optional": []},',
    '  "min_lookback_required": 252,',
    '  "extraction_source": "abstract+pdf"',
    '}',
    '```',
  ].join('\n');
  const out = orch._parseJSON(realFixture);
  assert.equal(out.strategy_id, 'S_regime_dependent_trend_following');
  assert.equal(out.rejection_reason_if_any, null);
});
