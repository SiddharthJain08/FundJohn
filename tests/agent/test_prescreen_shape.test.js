'use strict';

/**
 * tests/agent/test_prescreen_shape.test.js
 *
 * Guards `_isPrescreenShape` — the review-fix (Task R2, Minor finding) that
 * closes a hole in the orchestrator's factor-prescreen result handling.
 *
 * Pre-fix: `factor_prescreen.py`'s last stdout line was JSON.parse'd and
 * handled as `else if (psResult && psResult.pass === false) {block} else
 * {pass}` — ANY value that parses as JSON but isn't the expected
 * `{pass: bool, reason, stats}` shape (e.g. `5`, `{}`, `"ok"`, `null`,
 * `[]`) fell into the final `else` and was silently treated as a clean
 * pass, instead of the `prescreen_infra_fail` warn-and-pass path the
 * orchestrator uses for every other kind of infra trouble (non-zero exit,
 * unparseable stdout).
 *
 * Post-fix: research-orchestrator.js parses stdout, then requires
 * `_isPrescreenShape(parsed)` before trusting it as `psResult` — anything
 * that fails the shape check is routed into `psInfraFail` instead, so it
 * gets the same `prescreen_infra_fail` handling as an unparseable line.
 *
 * Run:
 *   node --test tests/agent/test_prescreen_shape.test.js
 */

process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';

const { test } = require('node:test');
const assert    = require('node:assert/strict');

const { _isPrescreenShape } = require('../../src/agent/research/research-orchestrator');

test('_isPrescreenShape: accepts a full pass verdict', () => {
  assert.equal(_isPrescreenShape({ pass: true, reason: null, stats: { n: 10 } }), true);
});

test('_isPrescreenShape: accepts a full block verdict', () => {
  assert.equal(_isPrescreenShape({ pass: false, reason: 'zero_signals', stats: {} }), true);
});

test('_isPrescreenShape: accepts pass=true/false with no reason/stats keys', () => {
  assert.equal(_isPrescreenShape({ pass: true }), true);
  assert.equal(_isPrescreenShape({ pass: false }), true);
});

test('_isPrescreenShape: rejects a bare number (the `5` case from review)', () => {
  assert.equal(_isPrescreenShape(5), false);
});

test('_isPrescreenShape: rejects an empty object (the `{}` case from review)', () => {
  assert.equal(_isPrescreenShape({}), false);
});

test('_isPrescreenShape: rejects a bare string (the `"ok"` case from review)', () => {
  assert.equal(_isPrescreenShape('ok'), false);
});

test('_isPrescreenShape: rejects null', () => {
  assert.equal(_isPrescreenShape(null), false);
});

test('_isPrescreenShape: rejects an array (JSON.parse("[]") / JSON.parse("[1,2]"))', () => {
  assert.equal(_isPrescreenShape([]), false);
  assert.equal(_isPrescreenShape([1, 2]), false);
});

test('_isPrescreenShape: rejects an object whose `pass` is not a boolean', () => {
  assert.equal(_isPrescreenShape({ pass: 'true' }), false);
  assert.equal(_isPrescreenShape({ pass: 1 }), false);
  assert.equal(_isPrescreenShape({ pass: null }), false);
  assert.equal(_isPrescreenShape({ reason: 'no pass key at all' }), false);
});

test('_isPrescreenShape: rejects undefined', () => {
  assert.equal(_isPrescreenShape(undefined), false);
});
