'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { groupByStrategy, computeDayPnlUsd } = require('../src/channels/api/positions_grouped');

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

// ── computeDayPnlUsd (Phase 1 follow-up #11) ─────────────────────────────────

test('computeDayPnlUsd returns USD delta for a fully-populated row', () => {
  // GLD example: today pnl_pct = -0.024434, prev = -0.016921
  // size = 0.022 (2.2% of NAV), nav = 100_000
  //   delta = -0.024434 - -0.016921 = -0.007513
  //   day_pnl_usd = -0.007513 × 0.022 × 100_000 = -16.5286
  const got = computeDayPnlUsd({
    today_pnl_pct: -0.024434,
    prev_pnl_pct:  -0.016921,
    position_size_pct: 0.022,
    nav: 100_000,
  });
  assert.ok(Math.abs(got - (-16.5286)) < 1e-3, `expected ~-16.5286, got ${got}`);
});

test('computeDayPnlUsd returns null when prev_pnl_pct is missing (brand-new position)', () => {
  assert.equal(
    computeDayPnlUsd({
      today_pnl_pct: 0.0021,
      prev_pnl_pct:  null,
      position_size_pct: 0.022,
      nav: 100_000,
    }),
    null,
  );
});

test('computeDayPnlUsd returns null when position_size_pct is NaN (sharpe_cadence drift guard)', () => {
  assert.equal(
    computeDayPnlUsd({
      today_pnl_pct: 0.01,
      prev_pnl_pct:  0.005,
      position_size_pct: NaN,
      nav: 100_000,
    }),
    null,
  );
});

test('computeDayPnlUsd returns null when NAV is missing (alpaca-down fallback)', () => {
  assert.equal(
    computeDayPnlUsd({
      today_pnl_pct: 0.01,
      prev_pnl_pct:  0.005,
      position_size_pct: 0.022,
      nav: null,
    }),
    null,
  );
});

test('computeDayPnlUsd handles SHORT (sign already in pnl_pct) — symmetric with LONG', () => {
  // SHORT where price ROSE: today_pnl_pct goes more negative
  // delta = -0.016329 - -0.009056 = -0.007273
  // day_pnl_usd = -0.007273 × 0.022 × 100_000 = -16.0006
  const got = computeDayPnlUsd({
    today_pnl_pct: -0.016329,
    prev_pnl_pct:  -0.009056,
    position_size_pct: 0.022,
    nav: 100_000,
  });
  assert.ok(got < 0 && Math.abs(got - (-16.0006)) < 1e-3, `expected ~-16.0006, got ${got}`);
});

test('groupByStrategy subtotal sums correctly across populated + null rows', () => {
  const positions = [
    { symbol: 'A', day_pnl_usd:  10,   strategy_id: 'mix' },
    { symbol: 'B', day_pnl_usd: -3.5,  strategy_id: 'mix' },
    { symbol: 'C', day_pnl_usd: null,  strategy_id: 'mix' },  // brand-new, no prev
    { symbol: 'D', day_pnl_usd: NaN,   strategy_id: 'mix' },  // NaN-poisoned
  ];
  const [g] = groupByStrategy(positions);
  // null and NaN both flow through `Number(r.day_pnl_usd) || 0` as 0,
  // so the populated rows alone drive the subtotal.
  assert.equal(g.subtotal_day_pnl_usd, 6.5);
  assert.equal(g.positions.length, 4);
});
