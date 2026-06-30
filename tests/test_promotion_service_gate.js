'use strict';
const assert = require('assert');
const { getPromotionThreshold, evaluatePromotionGate } = require('../src/lib/promotion_service');

// getPromotionThreshold — per class + fallback
assert.deepStrictEqual(getPromotionThreshold('equity'), { min_sharpe: 0.5, max_drawdown_pct: 20 });
assert.deepStrictEqual(getPromotionThreshold('option'), { min_sharpe: 0.80, max_drawdown_pct: 30 });
assert.deepStrictEqual(getPromotionThreshold('crypto'), { min_sharpe: 0.50, max_drawdown_pct: 70 });
assert.deepStrictEqual(getPromotionThreshold('weird'), { min_sharpe: 0.5, max_drawdown_pct: 20 }); // fallback
assert.deepStrictEqual(getPromotionThreshold(undefined), { min_sharpe: 0.5, max_drawdown_pct: 20 });

// mock dbQuery: first call = strategy_backtest_runs, second = strategy_registry fallback
function mkQuery(runRow, regRow) {
  return async (sql) => {
    if (/strategy_backtest_runs/.test(sql)) return { rows: runRow ? [runRow] : [] };
    if (/strategy_registry/.test(sql))      return { rows: regRow ? [regRow] : [] };
    return { rows: [] };
  };
}
(async () => {
  // canonical present, equity pass
  let g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.9, total_max_dd_pct: 10 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, true); assert.deepStrictEqual(g.failedGates, []);
  // equity sub-floor sharpe
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.4, total_max_dd_pct: 10 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false); assert.ok(g.failedGates.includes('sharpe'));
  // equity dd fail
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.9, total_max_dd_pct: 25 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false); assert.ok(g.failedGates.includes('max_dd'));
  // option stricter: sharpe 0.7 passes equity but FAILS option (0.80 floor)
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.7, total_max_dd_pct: 10 }), sid: 'x', instrumentClass: 'option', force: false });
  assert.strictEqual(g.pass, false); assert.ok(g.failedGates.includes('sharpe'));
  // crypto looser DD: 50% dd FAILS equity but PASSES crypto (0.70)
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 0.6, total_max_dd_pct: 50 }), sid: 'x', instrumentClass: 'crypto', force: false });
  assert.strictEqual(g.pass, true);
  // canonical NaN -> registry fallback used
  g = await evaluatePromotionGate({ dbQuery: mkQuery(null, { backtest_sharpe: 0.4, backtest_max_dd_pct: 5 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false); assert.ok(g.failedGates.includes('sharpe'));
  // force bypasses everything
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: -5, total_max_dd_pct: 90 }), sid: 'x', instrumentClass: 'equity', force: true });
  assert.strictEqual(g.pass, true);
  console.log('ok test_promotion_service_gate');
})();
