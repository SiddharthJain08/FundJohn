/**
 * SP-4: per-class corpus bucket floor — option<0.80 and crypto<0.70 are
 * capped at 'med' even when implementability would otherwise promote them.
 * Run: node --test tests/test_mastermind_class_floor.test.js
 */
process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';
const { test } = require('node:test');
const assert   = require('node:assert/strict');
const { applyClassBucketFloor } = require('../src/agent/curators/mastermind');

test('option below 0.80 capped at med', () => {
  assert.equal(applyClassBucketFloor('implementable_candidate', 'option', 0.72), 'med');
});
test('option at/above 0.80 unchanged', () => {
  assert.equal(applyClassBucketFloor('implementable_candidate', 'option', 0.81), 'implementable_candidate');
});
test('crypto below 0.70 capped at med', () => {
  assert.equal(applyClassBucketFloor('high', 'crypto', 0.61), 'med');
});
test('crypto at/above 0.70 unchanged', () => {
  assert.equal(applyClassBucketFloor('high', 'crypto', 0.71), 'high');
});
test('equity unaffected', () => {
  assert.equal(applyClassBucketFloor('implementable_candidate', 'equity', 0.10), 'implementable_candidate');
});
test('non-high buckets pass through', () => {
  assert.equal(applyClassBucketFloor('low', 'option', 0.10), 'low');
});
