// src/channels/api/strategy_row.js
// Pure builder for one /api/strategies row. Backtest-sourced; only
// last_signal_date + status are live. No open positions, no live P&L.
const { classifyDrift } = require('./strategy_drift');
function buildStrategyRow(x) {
  const run = x.run || {};
  const panel = x.panel || {};
  const bw = x.bestWorst || {};
  const act = run.avg_holding_days != null ? Number(run.avg_holding_days) : null;
  // ARR = mean per-trade return %, i.e. AVG(pnl_pct)*100 over backtest trades.
  // (NOT total_return_pct/total_trades — that divides a compounded total by the
  // trade count and collapses to ~0% for high-trade strategies.)
  const arr = bw.avg_pnl != null ? Number(bw.avg_pnl) * 100 : null;
  const adr = (arr != null && act) ? (arr / Math.max(1, act)) : null;
  return {
    strategy_id: x.sid,
    status: x.rec?.state || 'unknown',
    state: x.rec?.state || 'unknown',
    is_stale: x.isStale,
    regime_active: x.regimeActive,
    active_in_regimes: x.activeRegimes,
    eligible_regimes: x.eligRaw,
    current_regime: x.currentRegime,
    description: x.rec?.metadata?.description || '',
    // ── Backtest-sourced metrics ──
    sharpe: run.total_sharpe ?? null,
    sortino: run.total_sortino ?? null,
    calmar: run.total_calmar ?? null,
    effective_sharpe: panel.effective_sharpe ?? null,
    backtest_return_pct: run.total_return_pct ?? null,
    backtest_max_dd_pct: run.total_max_dd_pct ?? null,
    backtest_avg_pnl_pct: run.total_avg_pnl_pct ?? null,
    closed_count: run.total_trades ?? 0,
    win_rate: run.total_hit_rate ?? null,
    arr_pct: arr,
    adr_pct: adr,
    act_days: act,
    best_trade_pct: bw.best ?? null,
    worst_trade_pct: bw.worst ?? null,
    backtest_regime_breakdown: x.regimeBreakdown || {},
    oue_over: panel.oue_over ?? 0,
    oue_under: panel.oue_under ?? 0,
    oue_expected: panel.oue_expected ?? 0,
    oue_by_regime: panel.oue_by_regime || null,
    has_backtest_panel: !!x.panel,
    // Per-regime-scoped metric variants. ALL == the legacy top-level fields
    // (built by the caller from the same run/panel/bestWorst sources); ELIGIBLE
    // + single-regime scopes are blendScope() outputs. default_scope tells the
    // client which to show first. Absent → client falls back to top-level r.*.
    metrics_by_scope: x.metricsByScope || null,
    default_scope:    x.defaultScope || 'ALL',
    // ── Registry drift (read-only; never writes to manifest/registry) ──
    registry_status: x.registryStatus ?? null,
    drift: classifyDrift(x.rec?.state, x.registryStatus),
    // ── Fresh marker (2026-07-13): went live within the last 7 days —
    // newly auto-promoted strategies wear a ✨ fresh badge in the status
    // column (same additive pattern as the drift badge) until the next
    // weekly batch supersedes them. Pure manifest derivation (state_since),
    // so it self-expires with no state to maintain.
    fresh: freshFlag(x.rec),
    // ── Live (the ONLY live fields) ──
    last_signal_date: x.lastSignalDate ?? null,
  };
}
const FRESH_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;
function freshFlag(rec) {
  if (!rec || (rec.state !== 'live' && rec.state !== 'monitoring')) return false;
  const t = Date.parse(rec.state_since || '');
  return Number.isFinite(t) && (Date.now() - t) < FRESH_WINDOW_MS;
}
module.exports = { buildStrategyRow };
