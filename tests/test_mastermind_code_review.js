'use strict';
// Pure-logic tests for mastermind_code_review.js — the decision rules that
// drive MasterMind's strategy-code audit:
//   - selectConcerns: which backtest shapes get flagged (the <30-trade rule)
//   - validateVerdict: what Opus output we accept as a structured verdict
//
// Run: node --test tests/test_mastermind_code_review.js

const test = require('node:test');
const assert = require('node:assert');
const { selectConcerns, validateVerdict, decideKeep } = require('../src/agent/curators/mastermind_code_review');

test('selectConcerns flags low_trade when total_trades < 30', () => {
  assert.deepStrictEqual(selectConcerns({ total_trades: 5 }), ['low_trade']);
  assert.deepStrictEqual(selectConcerns({ total_trades: 29 }), ['low_trade']);
});

test('selectConcerns flags no_trades when total_trades is 0 or null', () => {
  assert.ok(selectConcerns({ total_trades: 0 }).includes('no_trades'));
  assert.ok(selectConcerns({ total_trades: null }).includes('no_trades'));
});

test('selectConcerns returns no low_trade flag at/above 30 trades', () => {
  assert.deepStrictEqual(selectConcerns({ total_trades: 30 }), []);
  assert.deepStrictEqual(selectConcerns({ total_trades: 250 }), []);
});

test('selectConcerns honours a custom threshold', () => {
  assert.deepStrictEqual(selectConcerns({ total_trades: 40 }, { lowTradeThreshold: 50 }), ['low_trade']);
});

test('validateVerdict rejects non-objects and bad verdict enums', () => {
  assert.strictEqual(validateVerdict(null), null);
  assert.strictEqual(validateVerdict('nope'), null);
  assert.strictEqual(validateVerdict({ verdict: 'banana' }), null);
});

test('validateVerdict accepts a well-formed verdict and normalises issues', () => {
  const v = validateVerdict({ strategy_id: 'S_x', verdict: 'fix_suggested' });
  assert.strictEqual(v.verdict, 'fix_suggested');
  assert.deepStrictEqual(v.issues, []);          // missing issues -> []
  assert.strictEqual(v.strategy_id, 'S_x');
});

test('validateVerdict preserves a structured issues array', () => {
  const v = validateVerdict({
    verdict: 'broken',
    issues: [{ severity: 'high', kind: 'no_signal', detail: 'signal never fires' }],
  });
  assert.strictEqual(v.issues.length, 1);
  assert.strictEqual(v.issues[0].kind, 'no_signal');
});

// ── decideKeep: the gated-apply non-regression gate ─────────────────────────

test('decideKeep keeps a valid fix when the before was a broken 0-trade strategy', () => {
  const r = decideKeep({ trades: 0, sharpe: null }, { trades: 45, sharpe: 0.6 });
  assert.strictEqual(r.keep, true);
});

test('decideKeep rejects a fix that regresses Sharpe vs a healthy before', () => {
  const r = decideKeep({ trades: 200, sharpe: 1.0 }, { trades: 180, sharpe: 0.7 });
  assert.strictEqual(r.keep, false);
});

test('decideKeep keeps a fix that improves Sharpe', () => {
  const r = decideKeep({ trades: 200, sharpe: 1.0 }, { trades: 210, sharpe: 1.3 });
  assert.strictEqual(r.keep, true);
});

test('decideKeep rejects when after has too few trades (below minTrades floor)', () => {
  const r = decideKeep({ trades: 0, sharpe: null }, { trades: 12, sharpe: 2.0 });
  assert.strictEqual(r.keep, false);
});

test('decideKeep rejects an after with non-finite Sharpe', () => {
  assert.strictEqual(decideKeep({ trades: 0, sharpe: null }, { trades: 99, sharpe: NaN }).keep, false);
  assert.strictEqual(decideKeep({ trades: 0, sharpe: null }, { trades: 99, sharpe: null }).keep, false);
});
