'use strict';
// Shared promotion gate + transition core. Single source both the dashboard
// /transition route AND the Discord /approve-strategy call, so the engine's
// trade-gate (strategy_registry.status='approved') can only be reached through
// the same class-aware quality gate (W4-F2 / W4-Tier3). Mirrors lifecycle.py
// PROMOTION_THRESHOLDS — keep in sync.
// Operator policy 2026-07-13 (v2, supersedes same-day per-regime v1): the
// gate is FULLY per-regime — a strategy is promotable when ANY regime sleeve
// of its latest primary backtest qualifies on ALL THREE per-sleeve gates:
//   sharpe  STRICTLY EXCEEDS min_sharpe (> 0 — "positive Sharpe"),
//   max_dd_pct of the SLEEVE ≤ max_drawdown_pct (same class values as before,
//     now judged per regime, not total-window),
//   trade_count of the SLEEVE ≥ min_trades (100).
// It then goes live in exactly its qualifying regimes. Post-promotion, live
// execution is gated by the activation min-Sharpe slider
// (pipeline_config.strategy_activation_min_sharpe → activation_assigner), so
// this entry gate is deliberately permissive; the slider is the risk dial.
const PROMOTION_THRESHOLDS = {
  equity: { min_sharpe: 0, max_drawdown_pct: 20, min_trades: 100 },
  etp:    { min_sharpe: 0, max_drawdown_pct: 20, min_trades: 100 },
  // option/crypto keep their looser DD ceilings (synthetic options engine /
  // BTC 60-80% DD asset — see lifecycle.py history). Sharpe floor is now the
  // shared ">0"; the option engine's uncertainty is carried by the slider.
  option: { min_sharpe: 0, max_drawdown_pct: 30, min_trades: 100 },
  crypto: { min_sharpe: 0, max_drawdown_pct: 70, min_trades: 100 },
};
const CANONICAL_REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
function getPromotionThreshold(instrumentClass) {
  return PROMOTION_THRESHOLDS[instrumentClass] || PROMOTION_THRESHOLDS.equity;
}
// Judge ONE regime sleeve row against the class thresholds. Returns the list
// of failed gate kinds ('no_backtest' | 'sharpe' | 'max_dd' | 'trades'); an
// empty list means the sleeve qualifies. Missing row / NULL metric fails
// closed as no_backtest — never a silent pass.
function judgeRegimeSleeve(row, thresholds) {
  const s  = row && row.sharpe      != null ? parseFloat(row.sharpe)          : NaN;
  const dd = row && row.max_dd_pct  != null ? parseFloat(row.max_dd_pct)      : NaN;
  const n  = row && row.trade_count != null ? parseInt(row.trade_count, 10)   : NaN;
  if (isNaN(s) || isNaN(dd) || isNaN(n)) return ['no_backtest'];
  const fails = [];
  if (!(s > thresholds.min_sharpe)) fails.push('sharpe');          // strict: must EXCEED
  if (dd > thresholds.max_drawdown_pct) fails.push('max_dd');
  if (n < thresholds.min_trades) fails.push('trades');
  return fails;
}
async function _latestPrimaryRun(dbQuery, sid) {
  let sharpe = NaN, maxDd = NaN, trades = NaN, runId = null, hasRun = false;
  try {
    const ubt = await dbQuery(
      `SELECT run_id, total_sharpe, total_max_dd_pct, total_trades FROM strategy_backtest_runs
        WHERE strategy_id = $1 AND primary_window = TRUE
        ORDER BY run_at DESC LIMIT 1`, [sid]);
    if (ubt.rows[0]) {
      hasRun = true;
      runId  = ubt.rows[0].run_id != null ? ubt.rows[0].run_id : null;
      sharpe = parseFloat(ubt.rows[0].total_sharpe);
      maxDd  = parseFloat(ubt.rows[0].total_max_dd_pct);
      trades = ubt.rows[0].total_trades != null ? parseInt(ubt.rows[0].total_trades, 10) : NaN;
    }
  } catch (_) {}
  return { hasRun, runId, sharpe, maxDd, trades };
}
async function _regimeSleeves(dbQuery, runId) {
  if (runId == null) return null;
  try {
    const rg = await dbQuery(
      `SELECT regime_state, sharpe, trade_count, max_dd_pct
         FROM strategy_backtest_regimes WHERE run_id = $1`, [runId]);
    return new Map(rg.rows.map(r => [r.regime_state, r]));
  } catch (_) { return null; }
}
// The set of regimes in which the strategy QUALIFIES for live activation,
// derived from the latest primary backtest's per-regime sleeves. This is the
// single source the fully-automatic research pipeline uses to decide both
// WHETHER to promote (non-empty set) and WHICH regimes to activate.
async function computeQualifyingRegimes({ dbQuery, sid, instrumentClass }) {
  const thresholds = getPromotionThreshold(instrumentClass);
  const run = await _latestPrimaryRun(dbQuery, sid);
  const out = { hasRun: run.hasRun, runId: run.runId, thresholds, qualifying: [], diag: {} };
  if (!run.hasRun) return out;
  const byRegime = await _regimeSleeves(dbQuery, run.runId);
  if (!byRegime) return out;                       // no sleeves recorded → nothing qualifies
  for (const regime of CANONICAL_REGIMES) {
    const row = byRegime.get(regime);
    if (!row) continue;                            // strategy never fired in this regime
    const fails = judgeRegimeSleeve(row, thresholds);
    out.diag[regime] = {
      sharpe: row.sharpe != null ? parseFloat(row.sharpe) : null,
      trade_count: row.trade_count != null ? parseInt(row.trade_count, 10) : null,
      max_dd_pct: row.max_dd_pct != null ? parseFloat(row.max_dd_pct) : null,
      failed: fails,
    };
    if (fails.length === 0) out.qualifying.push(regime);
  }
  return out;
}
async function evaluatePromotionGate({ dbQuery, sid, instrumentClass, force, eligibleRegimes }) {
  const thresholds = getPromotionThreshold(instrumentClass);
  if (force) return { pass: true, failedGates: [], sharpe: NaN, maxDd: NaN, thresholds, qualifyingRegimes: [] };
  const run = await _latestPrimaryRun(dbQuery, sid);
  const { sharpe, maxDd } = run;
  // strategy_backtest_runs (canonical) is the SOLE source for this gate
  // (registry mirror retired 2026-07-05, Option B). Missing/NaN canonical
  // metrics MUST fail closed via 'no_backtest' — never a silent pass.
  // force=true still bypasses everything.
  if (!run.hasRun) {
    return { pass: false, failedGates: ['no_backtest'], sharpe, maxDd, thresholds, qualifyingRegimes: [] };
  }
  const byRegime = await _regimeSleeves(dbQuery, run.runId);
  const named = Array.isArray(eligibleRegimes) && eligibleRegimes.length > 0;
  const failedGates = [];
  if (named) {
    // Caller names the activation set → EVERY named regime must qualify on
    // its own sleeve (all three gates). Fail tags are regime-qualified:
    // 'sharpe:CRISIS', 'max_dd:LOW_VOL', 'trades:HIGH_VOL', 'no_backtest:R'.
    const qualifying = [];
    for (const regime of eligibleRegimes) {
      const fails = byRegime ? judgeRegimeSleeve(byRegime.get(regime), thresholds) : ['no_backtest'];
      if (fails.length === 0) qualifying.push(regime);
      for (const f of fails) failedGates.push(`${f}:${regime}`);
    }
    return { pass: failedGates.length === 0, failedGates, sharpe, maxDd, thresholds, qualifyingRegimes: qualifying };
  }
  if (byRegime && byRegime.size > 0) {
    // Auto (regime-derived) mode: pass when ANY sleeve qualifies; the
    // qualifying set is returned so the caller can activate exactly those.
    const qualifying = [];
    const diagFails = [];
    for (const regime of CANONICAL_REGIMES) {
      const row = byRegime.get(regime);
      if (!row) continue;
      const fails = judgeRegimeSleeve(row, thresholds);
      if (fails.length === 0) qualifying.push(regime);
      else for (const f of fails) diagFails.push(`${f}:${regime}`);
    }
    if (qualifying.length > 0) {
      return { pass: true, failedGates: [], sharpe, maxDd, thresholds, qualifyingRegimes: qualifying };
    }
    return { pass: false, failedGates: ['no_qualifying_regime', ...diagFails], sharpe, maxDd, thresholds, qualifyingRegimes: [] };
  }
  // Legacy total-window fallback — only reachable when the run has no run_id
  // to join sleeves on, or recorded no per-regime sleeves at all (pre-regime
  // backtest rows). Same three gates on total-window metrics.
  if (isNaN(sharpe) || isNaN(maxDd) || isNaN(run.trades)) failedGates.push('no_backtest');
  else {
    if (!(sharpe > thresholds.min_sharpe)) failedGates.push('sharpe');
    if (maxDd > thresholds.max_drawdown_pct) failedGates.push('max_dd');
    if (run.trades < thresholds.min_trades) failedGates.push('trades');
  }
  return { pass: failedGates.length === 0, failedGates, sharpe, maxDd, thresholds, qualifyingRegimes: [] };
}
const REGISTRY_STATUS_FOR = { live:'approved', monitoring:'approved', paper:'pending_approval',
  candidate:'pending_approval', staging:'pending_approval', deprecated:'deprecated', archived:'deprecated' };

async function transitionStrategy({ dbQuery, manifestPath, sid, toState, fromState, force, actor, reason, instrumentClass, gateApplies, manifestMutator, eligibleRegimes }) {
  const { syncRegistryStatus } = require('../channels/api/registry_sync');
  const { withManifestLock } = require('./manifest_lock');
  // When the gate passes in auto mode (caller named no activation set), this
  // holds the gate-computed qualifying-regime set: it is written into the
  // manifest metadata below and returned so the route can sync
  // strategy_regime_params to exactly the regimes that earned activation.
  let autoQualifying = null;
  if (gateApplies && !force) {
    // Per-regime promotion gate (policy 2026-07-13 v2). A caller-named set
    // (dashboard picker) is judged as-is: every named regime must qualify.
    // No named set → auto mode: the gate derives the qualifying set from the
    // latest primary backtest's sleeves itself (the old manifest-metadata
    // derive is retired — the assigner's stale hint is strictly worse than
    // re-judging the same DB rows under the current policy).
    const g = await evaluatePromotionGate({ dbQuery, sid, instrumentClass, force: false, eligibleRegimes });
    if (!g.pass) return { ok: false, fromState, toState, weights_rebuild_triggered: false, failedGates: g.failedGates };
    if ((!Array.isArray(eligibleRegimes) || eligibleRegimes.length === 0) &&
        Array.isArray(g.qualifyingRegimes) && g.qualifyingRegimes.length > 0) {
      autoQualifying = g.qualifyingRegimes;
    }
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
      // Auto-mode activation set: persist the gate-computed qualifying
      // regimes as the manifest's eligibility hint (mirrors what the
      // dashboard picker's manifestMutator does for a named set), fold the
      // before/after into the audit event, and drop the legacy top-level
      // eligible_regimes copy (doctor's manifest_eligibility_drift check).
      if (autoQualifying) {
        r.metadata = r.metadata || {};
        const prior = Array.isArray(r.metadata.eligible_regimes) ? r.metadata.eligible_regimes.slice() : null;
        r.metadata.eligible_regimes = autoQualifying.slice();
        delete r.eligible_regimes;
        event.metadata = Object.assign({}, event.metadata || {}, {
          eligible_regimes_before: prior,
          eligible_regimes_after:  autoQualifying.slice(),
          eligible_regimes_source: 'gate_auto_qualifying',
        });
      }
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
  return { ok: true, fromState, toState, weights_rebuild_triggered, event,
           qualifyingRegimes: autoQualifying || (Array.isArray(eligibleRegimes) && eligibleRegimes.length ? eligibleRegimes.slice() : null) };
}
module.exports = { getPromotionThreshold, evaluatePromotionGate, computeQualifyingRegimes, judgeRegimeSleeve,
                   transitionStrategy, PROMOTION_THRESHOLDS, REGISTRY_STATUS_FOR, CANONICAL_REGIMES };
