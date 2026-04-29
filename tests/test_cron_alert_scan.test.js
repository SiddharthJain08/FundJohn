'use strict';

/**
 * tests/test_cron_alert_scan.test.js
 *
 * Verifies the [WARN]/[ERROR]/[FAIL]/FATAL pattern from
 * src/engine/cron-schedule.js correctly catches the silent-failure lines
 * that the 2026-04-28 LOW_VOL miss revealed (run_market_state.py
 * emitted `[WARN] DB write failed: schema "np" does not exist` to
 * stdout but the line was never surfaced to operators for 7 days).
 *
 * Run:
 *   node --test tests/test_cron_alert_scan.test.js
 */

const { test } = require('node:test');
const assert    = require('node:assert/strict');

// Replicate the regex exactly (kept private inside cron-schedule.js).
// If the source ever drifts, the test must be updated in lockstep.
const _ALERT_PATTERN = /^\s*(\[(WARN|ERROR|FAIL)\]|FATAL:)/i;

test('regex matches the actual silent-failure line that drifted regime for 7 days', () => {
  const real = '  [WARN] DB write failed: schema "np" does not exist';
  assert.ok(_ALERT_PATTERN.test(real),
            'must match the np-schema bug line that 2026-04-28 missed');
});

test('regex catches [WARN], [ERROR], [FAIL], FATAL: variants', () => {
  for (const line of [
    '[WARN] foo',
    '[warn] foo',           // case-insensitive
    '  [ERROR] db unreachable',
    '[FAIL] subprocess died',
    'FATAL: regime stale: 100h',
    'fatal: out of memory',
  ]) {
    assert.ok(_ALERT_PATTERN.test(line), `should match: ${JSON.stringify(line)}`);
  }
});

test('regex skips normal output and unrelated bracketed prefixes', () => {
  for (const line of [
    '[market-state] Step 8 — Syncing regime to PostgreSQL...',
    '[INFO] something',
    '[debug] trace info',
    '  Normal stdout line',
    '',
    '[CRON] cycle started',
    'WARN something but not bracketed',     // bare WARN should NOT match
    '   [DOCTOR] check passed',
  ]) {
    assert.ok(!_ALERT_PATTERN.test(line), `should NOT match: ${JSON.stringify(line)}`);
  }
});

test('regex anchors at line-start so trailing [WARN] in body text does not false-positive', () => {
  // Only the start of a line counts. Inline mentions don't fire.
  assert.ok(!_ALERT_PATTERN.test('something happened: [WARN] inline'),
            'inline [WARN] mid-line should not match');
});
