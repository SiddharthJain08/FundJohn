'use strict';

/**
 * tests/pipeline/test_parquet_store_timeouts.test.js
 *
 * D3 (2026-08-23): every parquet op ran under one 30 s SIGKILL timeout.
 * write_options appends to a 2.27 GB master and takes longer than that, so the
 * writer was SIGKILLed every cycle from 08-11 (9/9 daily logs), leaving 2.8 GB
 * of orphan *.tmp files and discarding the phase's work. Ops get a timeout
 * sized to what they actually do, overridable per op via env.
 *
 * Run: node --test tests/pipeline/test_parquet_store_timeouts.test.js
 */

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const ps = require(path.join(path.resolve(__dirname, '..', '..'), 'src/data/parquet_store.js'));

test('write_options gets a multi-minute timeout, small ops keep 30 s', () => {
  assert.equal(ps._opTimeoutMs('write_options', {}), 20 * 60_000);
  assert.equal(ps._opTimeoutMs('write_prices', {}), 30_000);
  assert.equal(ps._opTimeoutMs('row_count', {}), 30_000);
});

test('per-op env override PARQUET_OP_TIMEOUT_MS_<OP> wins, garbage is ignored', () => {
  assert.equal(ps._opTimeoutMs('write_options', { PARQUET_OP_TIMEOUT_MS_WRITE_OPTIONS: '90000' }), 90_000);
  assert.equal(ps._opTimeoutMs('write_prices', { PARQUET_OP_TIMEOUT_MS_WRITE_PRICES: '45000' }), 45_000);
  assert.equal(ps._opTimeoutMs('write_options', { PARQUET_OP_TIMEOUT_MS_WRITE_OPTIONS: 'nope' }), 20 * 60_000);
  assert.equal(ps._opTimeoutMs('write_options', { PARQUET_OP_TIMEOUT_MS_WRITE_OPTIONS: '0' }), 20 * 60_000);
});
