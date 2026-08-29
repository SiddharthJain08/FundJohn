// src/channels/api/blend_scope.js
// Pure blender: collapse per-regime backtest rows into a single metrics object
// for a chosen subset of regimes. JS port of universe_grid_cli.blend_metrics's
// rules, specialized to the dashboard's headline columns.
//
//   sharpe              : day-frequency-weighted (weight = oos_days_in_regime),
//                         skipping regimes with null sharpe; null if none.
//   max_dd_pct          : max over included regimes (percent units).
//   closed_count        : sum of trade_count over included regimes.
//   win_rate            : trade-count-weighted mean of hit_rate (regimes w/ trades).
//   arr_pct             : trade-count-weighted mean of avg_pnl_pct * 100.
//   act_days            : trade-count-weighted mean of avg_holding_days.
//   adr_pct             : arr_pct / max(1, act_days).
//   effective_sharpe    : raw sharpe by default (cadence normalization
//                         retired 2026-08-29, spec D2); sharpe / sqrt(act_days)
//                         only under OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1
//                         (matches backtest_panel — both raw by default).
//   return_pct          : day-freq-weighted mean of return_pct (informational).
//
// breakdown: { REGIME: { sharpe, max_dd_pct, return_pct, hit_rate, avg_pnl_pct,
//                        avg_holding_days, trade_count, oos_days_in_regime }, ... }
// regimeKeys: array of regimes to include.
// Returns a metrics object, or null when no included regime has a row.
function blendScope(breakdown, regimeKeys) {
  if (!breakdown || !Array.isArray(regimeKeys) || regimeKeys.length === 0) return null;
  const rows = regimeKeys.map(rg => breakdown[rg]).filter(Boolean);
  if (rows.length === 0) return null;

  // day-frequency-weighted mean over rows whose `key` is non-null
  const dayFreqWeighted = (key) => {
    const contrib = rows.filter(r => r[key] != null);
    if (!contrib.length) return null;
    let wsum = 0, vsum = 0;
    for (const r of contrib) {
      const w = Number(r.oos_days_in_regime) || 0;
      wsum += w; vsum += Number(r[key]) * w;
    }
    if (wsum < 1e-12) {
      // all weights zero → equal weight
      return contrib.reduce((a, r) => a + Number(r[key]), 0) / contrib.length;
    }
    return vsum / wsum;
  };

  // trade-count-weighted mean over rows whose `key` is non-null AND trade_count>0
  const tradeWeighted = (key) => {
    const contrib = rows.filter(r => r[key] != null && (Number(r.trade_count) || 0) > 0);
    if (!contrib.length) return null;
    let tc = 0, vsum = 0;
    for (const r of contrib) {
      const c = Number(r.trade_count) || 0;
      tc += c; vsum += Number(r[key]) * c;
    }
    return tc > 0 ? vsum / tc : null;
  };

  const closed_count = rows.reduce((a, r) => a + (Number(r.trade_count) || 0), 0);
  const max_dd_pct = rows.reduce((a, r) => Math.max(a, Number(r.max_dd_pct) || 0), 0);
  const sharpe = dayFreqWeighted('sharpe');
  const return_pct = dayFreqWeighted('return_pct');
  const win_rate = closed_count > 0 ? tradeWeighted('hit_rate') : null;
  const act_days = closed_count > 0 ? tradeWeighted('avg_holding_days') : null;
  const avg_pnl = closed_count > 0 ? tradeWeighted('avg_pnl_pct') : null;
  const arr_pct = avg_pnl != null ? avg_pnl * 100 : null;
  const adr_pct = (arr_pct != null && act_days) ? (arr_pct / Math.max(1, act_days)) : null;
  const effective_sharpe = sharpe != null
    ? (process.env.OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM === '1' && act_days && act_days > 0
        ? sharpe / Math.sqrt(act_days) : sharpe)
    : null;

  return {
    sharpe, effective_sharpe, return_pct, max_dd_pct,
    closed_count, win_rate, arr_pct, adr_pct, act_days,
  };
}

module.exports = { blendScope };
