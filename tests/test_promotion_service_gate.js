'use strict';
const assert = require('assert');
const { getPromotionThreshold, evaluatePromotionGate } = require('../src/lib/promotion_service');

// getPromotionThreshold — per class + fallback
assert.deepStrictEqual(getPromotionThreshold('equity'), { min_sharpe: 0.5, max_drawdown_pct: 20 });
assert.deepStrictEqual(getPromotionThreshold('option'), { min_sharpe: 0.80, max_drawdown_pct: 30 });
assert.deepStrictEqual(getPromotionThreshold('crypto'), { min_sharpe: 0.50, max_drawdown_pct: 70 });
assert.deepStrictEqual(getPromotionThreshold('weird'), { min_sharpe: 0.5, max_drawdown_pct: 20 }); // fallback
assert.deepStrictEqual(getPromotionThreshold(undefined), { min_sharpe: 0.5, max_drawdown_pct: 20 });

// mock dbQuery: only strategy_backtest_runs is read (the registry mirror is
// no longer consulted at all — Option B, 2026-07-05). A second arg is kept
// for call-shape compatibility with older tests but a query against
// strategy_registry must never be issued by evaluatePromotionGate anymore.
function mkQuery(runRow, regRowShouldNotBeQueried) {
  return async (sql) => {
    if (/strategy_backtest_runs/.test(sql)) return { rows: runRow ? [runRow] : [] };
    if (/strategy_registry/.test(sql)) {
      throw new Error('evaluatePromotionGate must not query strategy_registry (mirror retired)');
    }
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
  // canonical row entirely missing -> HARD fail 'no_backtest' (NOT a silent
  // pass, and NOT a registry fallback -- the mirror is retired 2026-07-05).
  g = await evaluatePromotionGate({ dbQuery: mkQuery(null), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false);
  assert.ok(g.failedGates.includes('no_backtest'), 'missing canonical row must hard-fail no_backtest');
  assert.ok(isNaN(g.sharpe) && isNaN(g.maxDd));
  // canonical row present but sharpe column itself is NULL/NaN -> still a
  // hard fail on no_backtest (partial-NaN canonical is still "no valid
  // backtest" for the metric that's missing).
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: null, total_max_dd_pct: 10 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false);
  assert.ok(g.failedGates.includes('no_backtest'));
  // canonical present and within floor -> passes cleanly, no no_backtest gate
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: 1.2, total_max_dd_pct: 8 }), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, true);
  assert.ok(!g.failedGates.includes('no_backtest'));
  assert.strictEqual(g.sharpe, 1.2); assert.strictEqual(g.maxDd, 8);
  // force=true bypasses everything, including a hard no_backtest case
  g = await evaluatePromotionGate({ dbQuery: mkQuery(null), sid: 'x', instrumentClass: 'equity', force: true });
  assert.strictEqual(g.pass, true); assert.deepStrictEqual(g.failedGates, []);
  g = await evaluatePromotionGate({ dbQuery: mkQuery({ total_sharpe: -5, total_max_dd_pct: 90 }), sid: 'x', instrumentClass: 'equity', force: true });
  assert.strictEqual(g.pass, true);
  console.log('ok test_promotion_service_gate');
})();
