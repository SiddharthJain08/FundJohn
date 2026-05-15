'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { groupByStrategy } = require('../src/channels/api/positions_grouped');

test('groups positions by strategy_id with per-group day_pnl_usd subtotal', () => {
  const positions = [
    { symbol: 'AAPL', qty: 100, day_pnl_usd: 50,  strategy_id: 'regime_blended_sizer_live' },
    { symbol: 'MSFT', qty: 50,  day_pnl_usd: -20, strategy_id: 'regime_blended_sizer_live' },
    { symbol: 'NVDA', qty: 25,  day_pnl_usd: 100, strategy_id: 'sharpe_cadence_path' },
    { symbol: 'TSLA', qty: 10,  day_pnl_usd: 5,   strategy_id: null },
  ];
  const out = groupByStrategy(positions);
  const keys = out.map(g => g.strategy_id).sort();
  assert.deepEqual(keys, ['(unattributed)', 'regime_blended_sizer_live', 'sharpe_cadence_path']);
  const live = out.find(g => g.strategy_id === 'regime_blended_sizer_live');
  assert.equal(live.positions.length, 2);
  assert.equal(live.subtotal_day_pnl_usd, 30);
});

test('groupByStrategy on empty input returns empty array', () => {
  assert.deepEqual(groupByStrategy([]), []);
});

test('groupByStrategy preserves row order within each group', () => {
  const positions = [
    { symbol: 'A', day_pnl_usd: 1, strategy_id: 'x' },
    { symbol: 'B', day_pnl_usd: 2, strategy_id: 'x' },
    { symbol: 'C', day_pnl_usd: 3, strategy_id: 'x' },
  ];
  const [g] = groupByStrategy(positions);
  assert.deepEqual(g.positions.map(p => p.symbol), ['A', 'B', 'C']);
});
