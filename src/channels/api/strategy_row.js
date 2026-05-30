// src/channels/api/strategy_row.js
// Pure builder for one /api/strategies row. Backtest-sourced; only
// last_signal_date + status are live. No open positions, no live P&L.
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
    effective_sharpe: panel.effective_sharpe ?? null,
    backtest_return_pct: run.total_return_pct ?? null,
    backtest_max_dd_pct: run.total_max_dd_pct ?? null,
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
    // ── Live (the ONLY live fields) ──
    last_signal_date: x.lastSignalDate ?? null,
  };
}
module.exports = { buildStrategyRow };
