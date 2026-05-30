// tests/test_api_strategies_backtest.test.js
// Contract test: the per-strategy object builder maps backtest sources and
// keeps only last_signal + status live. We test the pure mapper exported
// from server for testability.
const assert = require('assert');
const { buildStrategyRow } = require('../src/channels/api/strategy_row');

const row = buildStrategyRow({
  sid: 'S_x', rec: { state: 'live', metadata: {} },
  isStale: false, regimeActive: true, activeRegimes: ['LOW_VOL'],
  eligRaw: ['LOW_VOL'], currentRegime: 'LOW_VOL',
  run: { total_sharpe: 2.0, total_return_pct: 50, total_max_dd_pct: 12,
         total_trades: 120, total_hit_rate: 0.55, avg_holding_days: 4 },
  regimeBreakdown: { LOW_VOL: { sharpe: 1.8, trade_count: 80, return_pct: 30, hit_rate: 0.6 } },
  panel: { effective_sharpe: 1.0, oue_over: 10, oue_under: 5, oue_expected: 105,
           oue_by_regime: { LOW_VOL: { over:10, under:5, expected:65 } } },
  bestWorst: { best: 0.22, worst: -0.09, avg_pnl: 0.012 },
  lastSignalDate: '2026-05-28',
});

assert.strictEqual(row.sharpe, 2.0);
assert.strictEqual(row.effective_sharpe, 1.0);
assert.strictEqual(row.closed_count, 120);          // backtest trade count
assert.strictEqual(row.win_rate, 0.55);             // backtest hit rate
// ARR = mean per-trade return (AVG(pnl_pct) * 100), NOT total_return/trade_count.
assert.strictEqual(row.arr_pct, 1.2);               // 0.012 * 100
assert.strictEqual(row.adr_pct, 0.3);               // arr / max(1, act=4)
assert.strictEqual(row.oue_over, 10);
assert.strictEqual(row.last_signal_date, '2026-05-28');  // live
assert.strictEqual(row.status, 'live');
assert.ok(!('open_count' in row), 'open_count must be removed');
assert.ok(!('avg_unrealized_pct' in row), 'live unrealized removed');
assert.ok(!('oue_multipliers_by_regime' in row), 'oue multiplier removed');
console.log('ok');
