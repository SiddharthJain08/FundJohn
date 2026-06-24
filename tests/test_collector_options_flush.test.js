'use strict';

/**
 * tests/test_collector_options_flush.test.js
 *
 * Defense-in-depth for the EOD OOM: runOptions used to buffer the WHOLE day's
 * option chain (~405k contracts) in the node process before a single end-of-phase
 * flush. These pin the threshold-based incremental flush that bounds the node-side
 * buffer (the parquet WRITE was the 4GB hog — fixed separately in parquet_store —
 * but bounding the JS buffer is cheap insurance).
 *
 * Run: node --test tests/test_collector_options_flush.test.js
 */

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const collector = require(path.join(path.resolve(__dirname, '..'), 'src/pipeline/collector.js'));

test('optionsFlushThreshold defaults to 250000 and reads the env override', () => {
  assert.equal(collector._optionsFlushThreshold({}), 250000);
  assert.equal(collector._optionsFlushThreshold({ OPTIONS_FLUSH_ROW_THRESHOLD: '100000' }), 100000);
  assert.equal(collector._optionsFlushThreshold({ OPTIONS_FLUSH_ROW_THRESHOLD: '0' }), 0); // 0 disables
});

test('shouldFlushOptions triggers at/above the threshold only', () => {
  assert.equal(collector._shouldFlushOptions(249999, 250000), false);
  assert.equal(collector._shouldFlushOptions(250000, 250000), true);
  assert.equal(collector._shouldFlushOptions(400000, 250000), true);
});

test('shouldFlushOptions is disabled when threshold is 0 or negative', () => {
  assert.equal(collector._shouldFlushOptions(999999, 0), false);
  assert.equal(collector._shouldFlushOptions(999999, -1), false);
});
