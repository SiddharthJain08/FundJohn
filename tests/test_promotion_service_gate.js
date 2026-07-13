'use strict';
const assert = require('assert');
const { getPromotionThreshold, evaluatePromotionGate, computeQualifyingRegimes, judgeRegimeSleeve } = require('../src/lib/promotion_service');

// ── Thresholds (operator policy 2026-07-13 v2) ─────────────────────────────
// Sharpe must STRICTLY EXCEED min_sharpe (0 → "positive Sharpe"); max-DD is
// judged PER REGIME SLEEVE with the same class ceilings as before; every
// qualifying sleeve needs ≥ 100 trades.
assert.deepStrictEqual(getPromotionThreshold('equity'), { min_sharpe: 0, max_drawdown_pct: 20, min_trades: 100 });
assert.deepStrictEqual(getPromotionThreshold('etp'),    { min_sharpe: 0, max_drawdown_pct: 20, min_trades: 100 });
assert.deepStrictEqual(getPromotionThreshold('option'), { min_sharpe: 0, max_drawdown_pct: 30, min_trades: 100 });
assert.deepStrictEqual(getPromotionThreshold('crypto'), { min_sharpe: 0, max_drawdown_pct: 70, min_trades: 100 });
assert.deepStrictEqual(getPromotionThreshold('weird'),  { min_sharpe: 0, max_drawdown_pct: 20, min_trades: 100 }); // fallback
assert.deepStrictEqual(getPromotionThreshold(undefined), { min_sharpe: 0, max_drawdown_pct: 20, min_trades: 100 });

// judgeRegimeSleeve — the single-sleeve rule
{
  const thr = getPromotionThreshold('equity');
  assert.deepStrictEqual(judgeRegimeSleeve({ sharpe: 0.01, max_dd_pct: 10, trade_count: 100 }, thr), []);
  assert.deepStrictEqual(judgeRegimeSleeve({ sharpe: 0,    max_dd_pct: 10, trade_count: 100 }, thr), ['sharpe'],
    'sharpe exactly 0 must FAIL — gate is strictly positive');
  assert.deepStrictEqual(judgeRegimeSleeve({ sharpe: -0.2, max_dd_pct: 10, trade_count: 100 }, thr), ['sharpe']);
  assert.deepStrictEqual(judgeRegimeSleeve({ sharpe: 1.0,  max_dd_pct: 25, trade_count: 100 }, thr), ['max_dd']);
  assert.deepStrictEqual(judgeRegimeSleeve({ sharpe: 1.0,  max_dd_pct: 10, trade_count: 99  }, thr), ['trades']);
  assert.deepStrictEqual(judgeRegimeSleeve({ sharpe: 1.0,  max_dd_pct: null, trade_count: 100 }, thr), ['no_backtest']);
  assert.deepStrictEqual(judgeRegimeSleeve(undefined, thr), ['no_backtest']);
  // crypto's looser DD ceiling applies per sleeve
  assert.deepStrictEqual(judgeRegimeSleeve({ sharpe: 0.4, max_dd_pct: 55, trade_count: 241 }, getPromotionThreshold('crypto')), []);
}

// mock dbQuery: canonical strategy_backtest_runs + strategy_backtest_regimes
// only (registry mirror retired 2026-07-05 — a strategy_registry query must
// never be issued).
function mkQuery(runRow, regimeRows) {
  return async (sql) => {
    if (/strategy_backtest_runs/.test(sql)) return { rows: runRow ? [runRow] : [] };
    if (/strategy_backtest_regimes/.test(sql)) return { rows: regimeRows || [] };
    if (/strategy_registry/.test(sql)) {
      throw new Error('evaluatePromotionGate must not query strategy_registry (mirror retired)');
    }
    return { rows: [] };
  };
}
const RUN = (over) => Object.assign({ run_id: 7, total_sharpe: 1.0, total_max_dd_pct: 10, total_trades: 500 }, over);
const SLEEVE = (regime, sharpe, trades, dd) => ({ regime_state: regime, sharpe, trade_count: trades, max_dd_pct: dd });

(async () => {
  let g;

  // ── Auto (regime-derived) mode: no eligibleRegimes supplied ──────────────
  // ANY qualifying sleeve → pass, with the qualifying set returned.
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN({ total_sharpe: -0.85 }),
                     [SLEEVE('CRISIS', 3.25, 130, 12), SLEEVE('LOW_VOL', -0.95, 400, 8)]),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, true, 'one qualifying sleeve must pass despite negative total sharpe');
  assert.deepStrictEqual(g.qualifyingRegimes, ['CRISIS']);
  assert.deepStrictEqual(g.failedGates, []);

  // TOTAL max-DD no longer gates: total dd 49% but the qualifying sleeve's
  // own dd is fine → PASS (per-regime DD policy).
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN({ total_max_dd_pct: 49 }),
                     [SLEEVE('CRISIS', 3.25, 130, 12)]),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, true, 'total-window DD must not gate when the sleeve DD is within ceiling');
  assert.deepStrictEqual(g.qualifyingRegimes, ['CRISIS']);

  // No sleeve qualifies → fail with no_qualifying_regime + per-sleeve tags.
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN(),
                     [SLEEVE('LOW_VOL', 0.49, 40, 8), SLEEVE('CRISIS', -0.5, 200, 12)]),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false);
  assert.ok(g.failedGates.includes('no_qualifying_regime'));
  assert.ok(g.failedGates.includes('trades:LOW_VOL'));
  assert.ok(g.failedGates.includes('sharpe:CRISIS'));
  assert.deepStrictEqual(g.qualifyingRegimes, []);

  // A sleeve failing ONLY on its own DD is excluded from the qualifying set.
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN(),
                     [SLEEVE('LOW_VOL', 1.2, 300, 35), SLEEVE('HIGH_VOL', 0.6, 150, 15)]),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, true);
  assert.deepStrictEqual(g.qualifyingRegimes, ['HIGH_VOL'], 'LOW_VOL sleeve dd 35% > 20% must not qualify');

  // Qualifying set is returned in canonical regime order.
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN(),
                     [SLEEVE('CRISIS', 2.0, 120, 5), SLEEVE('LOW_VOL', 1.0, 200, 5), SLEEVE('TRANSITIONING', 0.5, 110, 5)]),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.deepStrictEqual(g.qualifyingRegimes, ['LOW_VOL', 'TRANSITIONING', 'CRISIS']);

  // ── Named-set mode: caller names the activation set → ALL must qualify ───
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN(), [SLEEVE('CRISIS', 3.25, 130, 12), SLEEVE('LOW_VOL', 0.49, 400, 8)]),
    sid: 'x', instrumentClass: 'equity', force: false, eligibleRegimes: ['CRISIS', 'LOW_VOL'] });
  assert.strictEqual(g.pass, true, 'sharpe 0.49 > 0 passes the positive-sharpe gate');
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN(), [SLEEVE('CRISIS', 3.25, 130, 12), SLEEVE('LOW_VOL', -0.1, 400, 8)]),
    sid: 'x', instrumentClass: 'equity', force: false, eligibleRegimes: ['CRISIS', 'LOW_VOL'] });
  assert.strictEqual(g.pass, false);
  assert.ok(g.failedGates.includes('sharpe:LOW_VOL'), 'non-positive sleeve must fail regime-tagged');
  assert.ok(!g.failedGates.includes('sharpe:CRISIS'));
  assert.deepStrictEqual(g.qualifyingRegimes, ['CRISIS'], 'named mode still reports which sleeves qualified');
  // per-regime trades gate
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN(), [SLEEVE('CRISIS', 3.25, 99, 12)]),
    sid: 'x', instrumentClass: 'equity', force: false, eligibleRegimes: ['CRISIS'] });
  assert.strictEqual(g.pass, false);
  assert.deepStrictEqual(g.failedGates, ['trades:CRISIS']);
  // per-regime DD gate
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN(), [SLEEVE('CRISIS', 3.25, 130, 28)]),
    sid: 'x', instrumentClass: 'equity', force: false, eligibleRegimes: ['CRISIS'] });
  assert.strictEqual(g.pass, false);
  assert.deepStrictEqual(g.failedGates, ['max_dd:CRISIS']);
  // ...but the same sleeve passes under option's 30% ceiling
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN(), [SLEEVE('CRISIS', 3.25, 130, 28)]),
    sid: 'x', instrumentClass: 'option', force: false, eligibleRegimes: ['CRISIS'] });
  assert.strictEqual(g.pass, true);
  // missing/NULL sleeve → fail closed per named regime
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN(), [SLEEVE('LOW_VOL', null, 400, 8)]),
    sid: 'x', instrumentClass: 'equity', force: false, eligibleRegimes: ['LOW_VOL', 'HIGH_VOL'] });
  assert.strictEqual(g.pass, false);
  assert.ok(g.failedGates.includes('no_backtest:LOW_VOL'));
  assert.ok(g.failedGates.includes('no_backtest:HIGH_VOL'));

  // ── Hard fail-closed cases ────────────────────────────────────────────────
  // canonical row entirely missing -> HARD fail 'no_backtest'
  g = await evaluatePromotionGate({ dbQuery: mkQuery(null), sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false);
  assert.deepStrictEqual(g.failedGates, ['no_backtest']);
  assert.ok(isNaN(g.sharpe) && isNaN(g.maxDd));

  // ── Legacy total-window fallback (run has no run_id / no sleeves) ────────
  g = await evaluatePromotionGate({
    dbQuery: mkQuery({ total_sharpe: 0.9, total_max_dd_pct: 10, total_trades: 500 }, []),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, true, 'legacy: positive total sharpe + dd + trades pass');
  g = await evaluatePromotionGate({
    dbQuery: mkQuery({ total_sharpe: 0, total_max_dd_pct: 10, total_trades: 500 }, []),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false);
  assert.ok(g.failedGates.includes('sharpe'), 'legacy: total sharpe must strictly exceed 0');
  g = await evaluatePromotionGate({
    dbQuery: mkQuery({ total_sharpe: 0.9, total_max_dd_pct: 25, total_trades: 500 }, []),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.ok(g.failedGates.includes('max_dd'));
  g = await evaluatePromotionGate({
    dbQuery: mkQuery({ total_sharpe: 0.9, total_max_dd_pct: 10, total_trades: 42 }, []),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.ok(g.failedGates.includes('trades'), 'legacy: total trades < 100 must fail');
  // partial-NaN canonical is still "no valid backtest"
  g = await evaluatePromotionGate({
    dbQuery: mkQuery({ total_sharpe: null, total_max_dd_pct: 10, total_trades: 500 }, []),
    sid: 'x', instrumentClass: 'equity', force: false });
  assert.strictEqual(g.pass, false);
  assert.ok(g.failedGates.includes('no_backtest'));

  // ── force=true bypasses everything ────────────────────────────────────────
  g = await evaluatePromotionGate({ dbQuery: mkQuery(null), sid: 'x', instrumentClass: 'equity', force: true });
  assert.strictEqual(g.pass, true); assert.deepStrictEqual(g.failedGates, []);
  g = await evaluatePromotionGate({
    dbQuery: mkQuery(RUN({ total_sharpe: -5, total_max_dd_pct: 90 }), [SLEEVE('LOW_VOL', -5, 3, 90)]),
    sid: 'x', instrumentClass: 'equity', force: true });
  assert.strictEqual(g.pass, true);

  // ── computeQualifyingRegimes ──────────────────────────────────────────────
  let q = await computeQualifyingRegimes({
    dbQuery: mkQuery(RUN(), [SLEEVE('CRISIS', 3.25, 130, 12), SLEEVE('LOW_VOL', 0.8, 40, 8), SLEEVE('HIGH_VOL', -0.2, 500, 10)]),
    sid: 'x', instrumentClass: 'equity' });
  assert.deepStrictEqual(q.qualifying, ['CRISIS']);
  assert.deepStrictEqual(q.diag.LOW_VOL.failed, ['trades']);
  assert.deepStrictEqual(q.diag.HIGH_VOL.failed, ['sharpe']);
  assert.strictEqual(q.hasRun, true);
  q = await computeQualifyingRegimes({ dbQuery: mkQuery(null), sid: 'x', instrumentClass: 'equity' });
  assert.strictEqual(q.hasRun, false);
  assert.deepStrictEqual(q.qualifying, []);

  console.log('ok test_promotion_service_gate');
})();
