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
  // Registry backtest_* mirror retired as a read-consumer 2026-07-05 (Option B
  // — see docs/superpowers/specs/2026-07-05-option-b-mirror-retirement-design.md).
  // strategy_backtest_runs (canonical) is now the SOLE source for this gate.
  // A missing/NaN canonical metric MUST be a hard fail, not a silent pass —
  // the pre-fix code's `if (!isNaN(sharpe) && sharpe < min)` treated NaN as
  // "no opinion", so a strategy with no valid backtest sailed through. Now it
  // fails closed via the 'no_backtest' gate. force=true still bypasses.
  const failedGates = [];
  if (isNaN(sharpe) || isNaN(maxDd)) failedGates.push('no_backtest');
  if (!isNaN(sharpe) && sharpe < thresholds.min_sharpe)    failedGates.push('sharpe');
  if (!isNaN(maxDd)  && maxDd  > thresholds.max_drawdown_pct) failedGates.push('max_dd');
  return { pass: failedGates.length === 0, failedGates, sharpe, maxDd, thresholds };
}
const REGISTRY_STATUS_FOR = { live:'approved', monitoring:'approved', paper:'pending_approval',
  candidate:'pending_approval', staging:'pending_approval', deprecated:'deprecated', archived:'deprecated' };

async function transitionStrategy({ dbQuery, manifestPath, sid, toState, fromState, force, actor, reason, instrumentClass, gateApplies, manifestMutator }) {
  const { syncRegistryStatus } = require('../channels/api/registry_sync');
  const { withManifestLock } = require('./manifest_lock');
  if (gateApplies && !force) {
    const g = await evaluatePromotionGate({ dbQuery, sid, instrumentClass, force: false });
    if (!g.pass) return { ok: false, fromState, toState, weights_rebuild_triggered: false, failedGates: g.failedGates };
  }
  const now = new Date().toISOString();
  const event = { from_state: fromState, to_state: toState, timestamp: now, actor,
                  reason: reason || `${fromState}->${toState}`, metadata: force ? { override: true } : {} };
  const targetStatus = REGISTRY_STATUS_FOR[toState];
  // C7-hole guard (W4-T3-C3 controller addendum): refuse an unknown toState
  // rather than silently writing the manifest with NO registry sync. All 7
  // lifecycle states are in REGISTRY_STATUS_FOR today; this only guards a
  // future-added state from slipping past the registry-first invariant.
  if (!(toState in REGISTRY_STATUS_FOR)) {
    return { ok: false, fromState, toState, weights_rebuild_triggered: false, error: `unknown toState '${toState}'` };
  }
  if (targetStatus) {
    try { await syncRegistryStatus({ dbQuery, sid, targetStatus, actor }); }
    catch (e) { return { ok: false, fromState, toState, weights_rebuild_triggered: false, error: `registry sync refused (nothing written): ${e.message}` }; }
  }
  try {
    await withManifestLock(manifestPath, (m) => {
      const r = (m.strategies || {})[sid];
      if (!r) throw new Error(`strategy ${sid} not in manifest`);
      r.state = toState; r.state_since = now; r.history = r.history || []; r.history.push(event);
      // Optional caller-supplied mutation inside the same lock (e.g. the
      // dashboard route's eligible_regimes cleanup + event.metadata fold).
      // Runs AFTER history.push so it can mutate the just-pushed event's
      // metadata, which the lifecycle_events insert below then persists.
      if (typeof manifestMutator === 'function') manifestMutator(r, event);
      m.updated_at = now; return m;
    }, { actor: `${actor || 'unknown'}` });
  } catch (e) { return { ok: false, fromState, toState, weights_rebuild_triggered: false, error: `manifest write failed (registry already ${targetStatus}; drift badge): ${e.message}` }; }
  try { await dbQuery(`INSERT INTO lifecycle_events (strategy_id, from_state, to_state, actor, reason, metadata) VALUES ($1,$2,$3,$4,$5,$6)`,
    [sid, fromState, toState, actor, event.reason, JSON.stringify(event.metadata)]); }
  catch (e) { console.warn('lifecycle_events insert failed (non-fatal):', e.message); }
  const ACTIVE = new Set(['live','monitoring']);
  const weights_rebuild_triggered = ACTIVE.has(fromState) !== ACTIVE.has(toState);
  return { ok: true, fromState, toState, weights_rebuild_triggered, event };
}
module.exports = { getPromotionThreshold, evaluatePromotionGate, transitionStrategy, PROMOTION_THRESHOLDS, REGISTRY_STATUS_FOR };
