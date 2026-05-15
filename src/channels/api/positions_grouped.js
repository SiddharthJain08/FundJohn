'use strict';

/**
 * Group positions by strategy_id.  Null/missing strategy_id collapses to '(unattributed)'.
 * Preserves input order within each group.  Adds subtotal_day_pnl_usd.
 *
 * Concept lifted from achannarasappa/ticker AssetGroup primitive — see
 * docs/superpowers/plans/2026-05-15-fincept-imports-master-plan.md (1E).
 *
 * @param {Array<{symbol:string, day_pnl_usd?:number, strategy_id?:string|null}>} positions
 * @returns {Array<{strategy_id:string, positions:Array, subtotal_day_pnl_usd:number}>}
 */
function groupByStrategy(positions) {
  const buckets = new Map();
  for (const p of positions) {
    const key = p.strategy_id || '(unattributed)';
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(p);
  }
  const out = [];
  for (const [strategy_id, rows] of buckets) {
    const subtotal = rows.reduce((s, r) => s + (Number(r.day_pnl_usd) || 0), 0);
    out.push({ strategy_id, positions: rows, subtotal_day_pnl_usd: subtotal });
  }
  return out;
}

module.exports = { groupByStrategy };
