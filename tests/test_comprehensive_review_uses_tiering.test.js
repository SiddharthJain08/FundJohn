'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ROOT = path.resolve(__dirname, '..');

test('comprehensive_review imports resolveModel', () => {
  const src = require('node:fs').readFileSync(
    path.join(ROOT, 'src/agent/curators/comprehensive_review.js'),
    'utf8'
  );
  assert.ok(
    src.includes("require('../config/resolve_model')") ||
    src.includes('require("../config/resolve_model")'),
    'comprehensive_review.js should require resolve_model'
  );
  assert.ok(
    src.includes("resolveModel('mastermind', 'comprehensive-review', 'memo_writer')"),
    'comprehensive_review.js should call resolveModel with the memo_writer node name'
  );
});
