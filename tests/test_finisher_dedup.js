#!/usr/bin/env node
'use strict';
/**
 * tests/test_finisher_dedup.js — unit tests for _isDuplicateCandidate
 *
 * Tests the shared helper with an injected execFileSync so no DB or Python
 * process is required.  Covers the four fail-open cases specified in W4-T3-C5.
 */

const { _isDuplicateCandidate } = require('../src/agent/curators/_candidate_dedup');

let passed = 0, failed = 0;

function assert(condition, msg) {
  if (condition) {
    console.log(`  PASS: ${msg}`);
    passed++;
  } else {
    console.error(`  FAIL: ${msg}`);
    failed++;
  }
}

// Silent log — suppress noise in test output
const log = () => {};

// ── Test 1: duplicate:true JSON → returns true
{
  const mockExec = () => JSON.stringify({ duplicate: true, reason: 'fingerprint_match', matches: [], threshold: 0.6 });
  const result = _isDuplicateCandidate(
    'S_momentum_test',
    {
      similarity_fingerprint: { formula_tokens: ['momentum', 'rv', 'decile'] },
      regime_applicability: ['LOW_VOL', 'TRANSITIONING'],
    },
    log,
    { execFileSync: mockExec }
  );
  assert(result === true, 'duplicate:true JSON → returns true');
}

// ── Test 2: duplicate:false JSON → returns false
{
  const mockExec = () => JSON.stringify({ duplicate: false, reason: null, matches: [], threshold: 0.6 });
  const result = _isDuplicateCandidate(
    'S_mean_reversion_new',
    {
      similarity_fingerprint: { formula_tokens: ['mean_reversion', 'z_score'] },
      regime_applicability: [],
    },
    log,
    { execFileSync: mockExec }
  );
  assert(result === false, 'duplicate:false JSON → returns false');
}

// ── Test 3: thrown error (timeout / non-JSON) → returns false (fail-open)
{
  const mockExec = () => { throw new Error('Command timed out after 30000ms'); };
  const result = _isDuplicateCandidate(
    'S_vol_regime',
    {
      similarity_fingerprint: { formula_tokens: ['volatility', 'regime'] },
      regime_applicability: ['HIGH_VOL'],
    },
    log,
    { execFileSync: mockExec }
  );
  assert(result === false, 'thrown error (timeout) → returns false (fail-open)');
}

// ── Test 4a: missing similarity_fingerprint entirely → returns false, no shell-out
{
  let shellOutCalled = false;
  const mockExec = () => { shellOutCalled = true; return '{}'; };
  const result = _isDuplicateCandidate(
    'S_no_fp',
    { regime_applicability: ['LOW_VOL'] }, // no similarity_fingerprint
    log,
    { execFileSync: mockExec }
  );
  assert(result === false, 'missing similarity_fingerprint → returns false');
  assert(!shellOutCalled, 'missing similarity_fingerprint → no shell-out attempted');
}

// ── Test 4b: similarity_fingerprint present but formula_tokens empty → fail-open
{
  let shellOutCalled = false;
  const mockExec = () => { shellOutCalled = true; return '{}'; };
  const result = _isDuplicateCandidate(
    'S_empty_tokens',
    {
      similarity_fingerprint: { formula_tokens: [] },
      regime_applicability: ['LOW_VOL'],
    },
    log,
    { execFileSync: mockExec }
  );
  assert(result === false, 'empty formula_tokens → returns false');
  assert(!shellOutCalled, 'empty formula_tokens → no shell-out attempted');
}

// ── Test 5: non-JSON response → returns false (fail-open via parse error)
{
  const mockExec = () => 'not-valid-json {{{{';
  const result = _isDuplicateCandidate(
    'S_bad_json',
    {
      similarity_fingerprint: { formula_tokens: ['alpha', 'beta'] },
      regime_applicability: ['any'],
    },
    log,
    { execFileSync: mockExec }
  );
  assert(result === false, 'non-JSON response → returns false (fail-open)');
}

// ── Test 6: regimes absent/null → passes 'any' to CLI (no crash)
{
  let capturedArgs = [];
  const mockExec = (_cmd, args) => {
    capturedArgs = args;
    return JSON.stringify({ duplicate: false, reason: null, matches: [] });
  };
  _isDuplicateCandidate(
    'S_no_regimes',
    {
      similarity_fingerprint: { formula_tokens: ['carry'] },
      // regime_applicability missing entirely
    },
    log,
    { execFileSync: mockExec }
  );
  const regimesIdx = capturedArgs.indexOf('--regimes');
  assert(regimesIdx >= 0 && capturedArgs[regimesIdx + 1] === 'any',
    'absent regime_applicability → passes "any" to CLI');
}

// ── Summary
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
