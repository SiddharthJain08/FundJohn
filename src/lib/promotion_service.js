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
// 2026-07-27 Calmar escape hatch on the DD leg (mirrors lifecycle.py): max DD
// is a running-max extreme that deepens mechanically with backtest duration /
// breadth, so the flat ceiling systematically killed long-history sleeves
// (momentum_12_1 LOW_VOL: Sharpe 2.62 on 4,759 trades, DD 26%). A sleeve
// whose Calmar >= min_calmar passes the DD leg up to dd_hard_cap_pct (the
// catastrophic ceiling that keeps martingale-shaped curves out regardless).
// R1 (2026-08-24, five-repo-adoptions): FOURTH sleeve leg, benchmark-
// relative. When the sleeve's persisted benchmark_sharpe (strategy_
// backtest_regimes.benchmark_sharpe, migration 149; regime-conditioned SPY
// Sharpe from src/backtest/benchmark_baseline.py) is present, ALSO require
// sharpe STRICTLY EXCEEDS benchmark_sharpe + MIN_EXCESS_SHARPE_VS_BENCHMARK
// (0.0 default). NULL benchmark_sharpe -> criterion skipped, fail open
// (logged). This leg only ever TIGHTENS the gate (see judgeRegimeSleeve).
const PROMOTION_THRESHOLDS = {
  equity: { min_sharpe: 0, max_drawdown_pct: 20, min_trades: 100, min_calmar: 0.5, dd_hard_cap_pct: 50 },
  etp:    { min_sharpe: 0, max_drawdown_pct: 20, min_trades: 100, min_calmar: 0.5, dd_hard_cap_pct: 50 },
  // option/crypto keep their looser DD ceilings (synthetic options engine /
  // BTC 60-80% DD asset — see lifecycle.py history). Sharpe floor is now the
  // shared ">0"; the option engine's uncertainty is carried by the slider.
  option: { min_sharpe: 0, max_drawdown_pct: 30, min_trades: 100, min_calmar: 0.5, dd_hard_cap_pct: 60 },
  crypto: { min_sharpe: 0, max_drawdown_pct: 70, min_trades: 100, min_calmar: 0.5, dd_hard_cap_pct: 85 },
};
// R1 (2026-08-24, five-repo-adoptions, benchmark-relative promotion
// criterion): mirrors lifecycle.py MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS —
// keep the literal 0.0 values in sync (tests/backtest/test_benchmark_baseline.py
// has a twin-sync tripwire). Kept as its OWN class-keyed object rather than
// folded into PROMOTION_THRESHOLDS, matching the python side's shape (see
// lifecycle.py's header comment on that dict for why: folding it in would
// change PROMOTION_THRESHOLDS's key set under existing exact-equality
// assertions). All four classes share the same 0.0 default for now.
const MIN_EXCESS_SHARPE_VS_BENCHMARK = 0;
const MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS = {
  equity: MIN_EXCESS_SHARPE_VS_BENCHMARK,
  etp:    MIN_EXCESS_SHARPE_VS_BENCHMARK,
  option: MIN_EXCESS_SHARPE_VS_BENCHMARK,
  crypto: MIN_EXCESS_SHARPE_VS_BENCHMARK,
};
const CANONICAL_REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
function getPromotionThreshold(instrumentClass) {
  return PROMOTION_THRESHOLDS[instrumentClass] || PROMOTION_THRESHOLDS.equity;
}
function getMinExcessSharpeVsBenchmark(instrumentClass) {
  return instrumentClass in MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS
    ? MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS[instrumentClass]
    : MIN_EXCESS_SHARPE_VS_BENCHMARK;
}
// Exit-hook spec §4: a backtest that flattened on BaseStrategy.should_exit
// must not go live until the live mirror (Phase 2) is enabled.
function exitHookLiveEnabled() { return process.env.OPENCLAW_EXIT_HOOK_LIVE === '1'; }
// Judge ONE regime sleeve row against the class thresholds. Returns the list
// of failed gate kinds ('no_backtest' | 'sharpe' | 'max_dd' | 'trades' |
// 'benchmark_sharpe'); an empty list means the sleeve qualifies. Missing row
// / NULL metric fails closed as no_backtest — never a silent pass.
//
// `ctx` (optional) carries {instrumentClass, sid, regime} for the R1
// benchmark leg + its log lines: instrumentClass selects the per-class
// excess-Sharpe floor (falls back to 'equity', matching getPromotionThreshold);
// sid/regime only name the log lines. Omitting ctx (as the pre-R1 test
// suite's 2-arg calls do) degrades gracefully — equity floor, '?' placeholders
// — with no change to the legacy fails[] behavior.
function judgeRegimeSleeve(row, thresholds, ctx = {}) {
  const { instrumentClass = 'equity', sid = '?', regime = '?' } = ctx;
  const s  = row && row.sharpe      != null ? parseFloat(row.sharpe)          : NaN;
  const dd = row && row.max_dd_pct  != null ? parseFloat(row.max_dd_pct)      : NaN;
  const n  = row && row.trade_count != null ? parseInt(row.trade_count, 10)   : NaN;
  if (isNaN(s) || isNaN(dd) || isNaN(n)) return ['no_backtest'];
  // Calmar escape hatch (2026-07-27): NULL/missing calmar forfeits only the
  // hatch — the flat ceiling still applies (never a silent pass).
  const cal = row && row.calmar != null ? parseFloat(row.calmar) : NaN;
  const ddOk = dd <= thresholds.max_drawdown_pct
    || (!isNaN(cal)
        && thresholds.min_calmar != null
        && cal >= thresholds.min_calmar
        && dd <= thresholds.dd_hard_cap_pct);
  const fails = [];
  if (!(s > thresholds.min_sharpe)) fails.push('sharpe');          // strict: must EXCEED
  if (!ddOk) fails.push('max_dd');
  if (n < thresholds.min_trades) fails.push('trades');
  const legacyPass = fails.length === 0;

  // R1 (2026-08-24, five-repo-adoptions): benchmark-relative promotion
  // criterion. `row.benchmark_sharpe` is the sleeve's persisted
  // strategy_backtest_regimes.benchmark_sharpe (migration 149), written by
  // unified_backtest.py from backtest.benchmark_baseline.regime_benchmark_sharpe.
  // pg's node-postgres driver returns NUMERIC columns as strings; parseFloat
  // + isNaN handles both a missing column (undefined -> NaN) and a
  // non-finite persisted value the same way: skip, fail open.
  const bench = row && row.benchmark_sharpe != null ? parseFloat(row.benchmark_sharpe) : NaN;
  if (isNaN(bench)) {
    // [bench_gate] no benchmark for <regime>; skipped -- logged every
    // evaluation (First-Sunday observability requirement), independent of
    // the legacy verdict.
    console.debug(`[bench_gate] no benchmark for ${regime}; skipped`);
    return fails;
  }
  const minExcess = getMinExcessSharpeVsBenchmark(instrumentClass);
  if (!(s > bench + minExcess)) fails.push('benchmark_sharpe');
  const benchPass = fails.length === 0;
  // Benchmark is ANDed on top of the legacy result, so it can only ever
  // TIGHTEN the gate (legacyPass=true, benchPass=false) — the reverse flip
  // is unreachable by construction; a comment here beats an assert inside a
  // live promotion gate.
  const _msg = `[bench_gate] ${sid} ${regime} legacy=${legacyPass ? 'PASS' : 'FAIL'} `
    + `bench=${benchPass ? 'PASS' : 'FAIL'} (sharpe=${s} bench=${bench})`;
  if (legacyPass !== benchPass) console.log(_msg);       // outcome changed: operator-visible
  else console.debug(_msg);                              // unchanged: still logged, quieter
  return fails;
}
async function _latestPrimaryRun(dbQuery, sid) {
  let sharpe = NaN, maxDd = NaN, trades = NaN, runId = null, hasRun = false, exitHook = false, maxHoldDays = null;
  try {
    const ubt = await dbQuery(
      `SELECT run_id, total_sharpe, total_max_dd_pct, total_trades, config_json FROM strategy_backtest_runs
        WHERE strategy_id = $1 AND primary_window = TRUE
        ORDER BY run_at DESC LIMIT 1`, [sid]);
    if (ubt.rows[0]) {
      hasRun = true;
      runId  = ubt.rows[0].run_id != null ? ubt.rows[0].run_id : null;
      sharpe = parseFloat(ubt.rows[0].total_sharpe);
      maxDd  = parseFloat(ubt.rows[0].total_max_dd_pct);
      trades = ubt.rows[0].total_trades != null ? parseInt(ubt.rows[0].total_trades, 10) : NaN;
      let cfg = ubt.rows[0].config_json;
      // A config_json we cannot parse tells us NOTHING about whether the run
      // leaned on the exit hook. Every other unknown in this gate fails closed
      // (no_backtest, missing sleeves); this one must too, or a corrupt row is
      // the single path that promotes a hook run onto a book that cannot honor
      // it. force:true still bypasses, like every other gate.
      let cfgBroken = false;
      if (typeof cfg === 'string') {
        try { cfg = JSON.parse(cfg); } catch (_) { cfg = null; cfgBroken = true; }
      }
      exitHook = cfgBroken || !!(cfg && cfg.exit_hook === true);
      // I5: the hold cap the RUN was measured at. NULL when unrecorded (or
      // when config_json would not parse) — the guard below then has nothing
      // to compare and stays out of the way.
      const mh = cfg && cfg.max_hold_days != null ? parseInt(cfg.max_hold_days, 10) : NaN;
      maxHoldDays = Number.isFinite(mh) ? mh : null;
    }
  } catch (_) {}
  return { hasRun, runId, sharpe, maxDd, trades, exitHook, maxHoldDays };
}
// The hold cap the LIVE exit-hook time stop will actually apply — JS twin of
// execution/regime_param_resolver.configured_max_hold_days: MAX of the
// non-null per-regime strategy_regime_params.max_hold_days, but only when the
// coupling gate OPENCLAW_BACKTEST_COUPLED_RECS=1; otherwise (and on any
// lookup failure, and when nothing is set) the resolver's default of 21.
const LIVE_HOLD_CAP_DEFAULT = 21;
async function _liveHoldCap(dbQuery, sid) {
  if (process.env.OPENCLAW_BACKTEST_COUPLED_RECS !== '1') return LIVE_HOLD_CAP_DEFAULT;
  try {
    const r = await dbQuery(
      `SELECT MAX(max_hold_days) AS m FROM strategy_regime_params WHERE strategy_id = $1`, [sid]);
    const m = r && r.rows && r.rows[0] && r.rows[0].m != null ? parseInt(r.rows[0].m, 10) : NaN;
    return Number.isFinite(m) ? m : LIVE_HOLD_CAP_DEFAULT;
  } catch (_) { return LIVE_HOLD_CAP_DEFAULT; }
}
// I5 (final review 2026-08-28): an exit_hook run measured at a different hold
// cap than the live time stop will apply is not the strategy that was judged.
// X1's qualifying run was pinned `--max-hold-days 30` while the live resolver
// returns 21 (no strategy_regime_params row) — 23 of the replay's live-only
// closes came from exactly that gap. Refuse rather than promote a mismatch;
// the operator aligns one side and re-runs. Only checked for exit_hook runs
// that RECORDED a cap: nothing to compare is not a failure.
async function _holdCapMismatch(dbQuery, sid, run) {
  if (!run.exitHook || run.maxHoldDays == null) return false;
  const live = await _liveHoldCap(dbQuery, sid);
  if (run.maxHoldDays === live) return false;
  console.log(`[exit_hook_gate] ${sid} run max_hold_days=${run.maxHoldDays} != live hold cap ${live}`);
  return true;
}
async function _regimeSleeves(dbQuery, runId) {
  if (runId == null) return null;
  try {
    const rg = await dbQuery(
      `SELECT regime_state, sharpe, trade_count, max_dd_pct, calmar, benchmark_sharpe
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
  if (run.exitHook && !exitHookLiveEnabled()) { out.exit_hook_live_disabled = true; return out; }
  if (await _holdCapMismatch(dbQuery, sid, run)) { out.exit_hook_hold_cap_mismatch = true; return out; }
  const byRegime = await _regimeSleeves(dbQuery, run.runId);
  if (!byRegime) return out;                       // no sleeves recorded → nothing qualifies
  for (const regime of CANONICAL_REGIMES) {
    const row = byRegime.get(regime);
    if (!row) continue;                            // strategy never fired in this regime
    const fails = judgeRegimeSleeve(row, thresholds, { instrumentClass, sid, regime });
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
  if (run.exitHook && !exitHookLiveEnabled()) {
    return { pass: false, failedGates: ['exit_hook_live_disabled'], sharpe, maxDd, thresholds, qualifyingRegimes: [] };
  }
  if (await _holdCapMismatch(dbQuery, sid, run)) {
    return { pass: false, failedGates: ['exit_hook_hold_cap_mismatch'], sharpe, maxDd, thresholds, qualifyingRegimes: [] };
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
      const fails = byRegime ? judgeRegimeSleeve(byRegime.get(regime), thresholds, { instrumentClass, sid, regime }) : ['no_backtest'];
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
      const fails = judgeRegimeSleeve(row, thresholds, { instrumentClass, sid, regime });
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
                   transitionStrategy, PROMOTION_THRESHOLDS, REGISTRY_STATUS_FOR, CANONICAL_REGIMES,
                   getMinExcessSharpeVsBenchmark, MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS };
