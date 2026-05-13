'use strict';

/**
 * tests/test_comprehensive_review_addenda_injection.test.js
 *
 * Phase 2F: verifies the prompt-prepend behavior in
 * src/agent/curators/comprehensive_review.js.
 *
 * Run:
 *   node --test tests/test_comprehensive_review_addenda_injection.test.js
 */

const { test } = require('node:test');
const assert    = require('node:assert/strict');

const cr = require('../src/agent/curators/comprehensive_review');

test('buildAddendaPrefix returns empty string when no addenda', () => {
  assert.equal(cr.buildAddendaPrefix([]), '');
  assert.equal(cr.buildAddendaPrefix(null), '');
  assert.equal(cr.buildAddendaPrefix(undefined), '');
});

test('buildAddendaPrefix prepends a labeled section with all addenda', () => {
  const addenda = [
    { id: 1, addendum_text: 'Discount 0.8 bucket — overconfident.' },
    { id: 2, addendum_text: 'Take stronger calls in 0.2 bucket.' },
  ];
  const out = cr.buildAddendaPrefix(addenda);
  assert.ok(out.startsWith('## Calibration addenda'));
  assert.ok(out.includes('Discount 0.8 bucket'));
  assert.ok(out.includes('Take stronger calls'));
  // Trailing double newline to separate from the main prompt body
  assert.ok(out.endsWith('\n\n'));
});

test('buildAddendaPrefix preserves order across multiple addenda', () => {
  const addenda = [
    { id: 1, addendum_text: 'FIRST' },
    { id: 2, addendum_text: 'SECOND' },
    { id: 3, addendum_text: 'THIRD' },
  ];
  const out = cr.buildAddendaPrefix(addenda);
  const idxFirst  = out.indexOf('FIRST');
  const idxSecond = out.indexOf('SECOND');
  const idxThird  = out.indexOf('THIRD');
  assert.ok(idxFirst < idxSecond);
  assert.ok(idxSecond < idxThird);
});

test('loadActiveCalibrationAddenda returns an array (possibly empty)', () => {
  // Real DB call — verifies wiring + JSON parse path. With no active
  // rows in test DB, returns [].
  const result = cr.loadActiveCalibrationAddenda();
  assert.ok(Array.isArray(result));
});
