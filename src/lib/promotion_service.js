'use strict';
// Shared promotion gate + transition core. Single source both the dashboard
// /transition route AND the Discord /approve-strategy call, so the engine's
// trade-gate (strategy_registry.status='approved') can only be reached through
// the same class-aware quality gate (W4-F2 / W4-Tier3). Mirrors lifecycle.py
// PROMOTION_THRESHOLDS — keep in sync.
const PROMOTION_THRESHOLDS = {
  equity: { min_sharpe: 0.5,  max_drawdown_pct: 20 },
  etp:    { min_sharpe: 0.5,  max_drawdown_pct: 20 },
  option: { min_sharpe: 0.80, max_drawdown_pct: 30 },
  crypto: { min_sharpe: 0.50, max_drawdown_pct: 70 },
};
function getPromotionThreshold(instrumentClass) {
  return PROMOTION_THRESHOLDS[instrumentClass] || PROMOTION_THRESHOLDS.equity;
}
async function evaluatePromotionGate({ dbQuery, sid, instrumentClass, force }) {
  const thresholds = getPromotionThreshold(instrumentClass);
  if (force) return { pass: true, failedGates: [], sharpe: NaN, maxDd: NaN, thresholds };
  let sharpe = NaN, maxDd = NaN;
  try {
    const ubt = await dbQuery(
      `SELECT total_sharpe, total_max_dd_pct FROM strategy_backtest_runs
        WHERE strategy_id = $1 AND primary_window = TRUE
        ORDER BY run_at DESC LIMIT 1`, [sid]);
    if (ubt.rows[0]) { sharpe = parseFloat(ubt.rows[0].total_sharpe); maxDd = parseFloat(ubt.rows[0].total_max_dd_pct); }
  } catch (_) {}
  if (isNaN(sharpe) || isNaN(maxDd)) {
    try {
      const sr = (await dbQuery(`SELECT backtest_sharpe, backtest_max_dd_pct FROM strategy_registry WHERE id = $1`, [sid])).rows[0] || {};
      if (isNaN(sharpe)) sharpe = parseFloat(sr.backtest_sharpe);
      if (isNaN(maxDd))  maxDd  = parseFloat(sr.backtest_max_dd_pct);
    } catch (_) {}
  }
  const failedGates = [];
  if (!isNaN(sharpe) && sharpe < thresholds.min_sharpe)    failedGates.push('sharpe');
  if (!isNaN(maxDd)  && maxDd  > thresholds.max_drawdown_pct) failedGates.push('max_dd');
  return { pass: failedGates.length === 0, failedGates, sharpe, maxDd, thresholds };
}
module.exports = { getPromotionThreshold, evaluatePromotionGate, PROMOTION_THRESHOLDS };
