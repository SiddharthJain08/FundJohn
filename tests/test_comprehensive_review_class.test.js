/**
 * SP-4: buildStrategyPrompt surfaces instrument_class + its promotion floor.
 * Run: node --test tests/test_comprehensive_review_class.test.js
 */
process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';
const { test } = require('node:test');
const assert   = require('node:assert/strict');
const { buildStrategyPrompt } = require('../src/agent/curators/comprehensive_review');

const emptyPack = { signals: [], pnl: [] };

test('option strategy prompt names the per-regime option floor (MaxDD 30%)', () => {
  // The old flat 0.80 Sharpe floor was replaced by the per-regime sleeve gate
  // (2026-07-14): Sharpe > 0, class MaxDD cap, >= 100 trades per regime.
  const s = { id: 'S_x', name: 'X', status: 'live', tier: 2, backtest_sharpe: 0.6,
              backtest_return_pct: 5, backtest_max_dd_pct: 10, universe: [],
              signal_frequency: 'daily', parameters: {}, regime_conditions: {},
              instrument_class: 'option' };
  const p = buildStrategyPrompt(s, emptyPack, []);
  assert.match(p, /option/);
  assert.match(p, /MaxDD ≤ 30%/);
  assert.match(p, /trades ≥ 100/);
});

test('absent instrument_class defaults to equity in the prompt', () => {
  const s = { id: 'S_y', name: 'Y', status: 'live', tier: 2, backtest_sharpe: 0.6,
              backtest_return_pct: 5, backtest_max_dd_pct: 10, universe: [],
              signal_frequency: 'daily', parameters: {}, regime_conditions: {} };
  const p = buildStrategyPrompt(s, emptyPack, []);
  assert.match(p, /equity/);
});
