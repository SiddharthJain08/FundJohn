'use strict';

/**
 * tests/pipeline/test_collector_fmp_errors.test.js
 *
 * D1 (2026-08-23): the fundamentals phase treated EVERY HTTP 402 from FMP as
 * "daily quota exhausted" and stopped the phase. On the Starter tier a 402 is
 * also how FMP refuses tier-gated SYMBOLS (preferreds / warrants / units —
 * body "Premium Query Parameter: 'Special Endpoint : This value set for
 * 'symbol' is not available under your current subscription"). Those sort
 * right after the first ~30 "A…" names, so the phase halted at ~30 tickers
 * every cycle from ≥08-13 and financials.parquet froze at period-end 07-15.
 *
 * Run: node --test tests/pipeline/test_collector_fmp_errors.test.js
 */

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');

const collector = require(path.join(path.resolve(__dirname, '..', '..'), 'src/pipeline/collector.js'));

const GATED_BODY = "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available under your current subscription. Please visit our documentation";

test('_httpError carries status and a truncated body on the thrown error', () => {
  const err = collector._httpError(402, GATED_BODY);
  assert.ok(err instanceof Error);
  assert.equal(err.message, 'HTTP 402');          // message shape unchanged for existing callers
  assert.equal(err.status, 402);
  assert.ok(err.body.startsWith('Premium Query Parameter'));
  assert.ok(err.body.length <= 300);
});

test('_classifyFmpError: 402 with a tier-gated-symbol body is symbol_gated, not quota', () => {
  assert.equal(collector._classifyFmpError(collector._httpError(402, GATED_BODY)), 'symbol_gated');
  assert.equal(collector._classifyFmpError(collector._httpError(402, 'Special Endpoint: not available under your current subscription')), 'symbol_gated');
});

test('_classifyFmpError: 402 without a symbol-gate body stays quota (legacy behaviour)', () => {
  assert.equal(collector._classifyFmpError(collector._httpError(402, '')), 'quota');
  assert.equal(collector._classifyFmpError(new Error('HTTP 402')), 'quota');   // bare legacy error
});

test('_classifyFmpError: 429 → rate_limited, 404 → not_found, anything else → other', () => {
  assert.equal(collector._classifyFmpError(new Error('HTTP 429 — rate limited by financialmodelingprep.com')), 'rate_limited');
  assert.equal(collector._classifyFmpError(collector._httpError(404, '')), 'not_found');
  assert.equal(collector._classifyFmpError(new Error('HTTP 404')), 'not_found');
  assert.equal(collector._classifyFmpError(new Error('Request timeout')), 'other');
  assert.equal(collector._classifyFmpError(collector._httpError(500, 'boom')), 'other');
});

test('_capScope keeps the first N tickers and reports how many were deferred', () => {
  const r = collector._capScope(['A', 'B', 'C', 'D'], 2);
  assert.deepEqual(r.tickers, ['A', 'B']);
  assert.equal(r.deferred, 2);
  const all = collector._capScope(['A', 'B'], 0);              // 0 / negative = no cap
  assert.deepEqual(all.tickers, ['A', 'B']);
  assert.equal(all.deferred, 0);
});

// ── D3: in-cycle options fetch is opt-in; the 16:30 ET archive owns the chain ──
test('_inCycleOptionsEnabled is off by default and on only for OPENCLAW_COLLECT_OPTIONS_INCYCLE=1', () => {
  assert.equal(collector._inCycleOptionsEnabled({}), false);
  assert.equal(collector._inCycleOptionsEnabled({ OPENCLAW_COLLECT_OPTIONS_INCYCLE: '0' }), false);
  assert.equal(collector._inCycleOptionsEnabled({ OPENCLAW_COLLECT_OPTIONS_INCYCLE: 'true' }), false);
  assert.equal(collector._inCycleOptionsEnabled({ OPENCLAW_COLLECT_OPTIONS_INCYCLE: '1' }), true);
});

// ── D4: insider per-symbol walk is a weekly reconciliation, not a daily sweep ──
test('_insiderWalkScope drops tickers checked recently, caps the rest, counts both', () => {
  const fresh = new Set(['B', 'D']);
  const r = collector._insiderWalkScope(['A', 'B', 'C', 'D', 'E'], fresh, 2);
  assert.deepEqual(r.tickers, ['A', 'C']);
  assert.equal(r.skippedFresh, 2);
  assert.equal(r.deferred, 1);
});

test('_insiderWalkScope with no cap and nothing fresh returns everything', () => {
  const r = collector._insiderWalkScope(['A', 'B'], new Set(), 0);
  assert.deepEqual(r.tickers, ['A', 'B']);
  assert.equal(r.skippedFresh, 0);
  assert.equal(r.deferred, 0);
});
