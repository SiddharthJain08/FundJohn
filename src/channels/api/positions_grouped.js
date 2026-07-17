'use strict';

/**
 * Compute the daily-mark dollar P&L for a single position row.
 *
 * Both inputs (`today_pnl_pct`, `prev_pnl_pct`) are *cumulative-since-entry*
 * fractions with the direction sign already baked in (so a SHORT whose price
 * rose carries a NEGATIVE pnl_pct). Their delta is the day's contribution
 * relative to entry, multiplied by the deployed notional (position_size_pct
 * × NAV) to yield USD.
 *
 * Equivalent to `(current - prev_close) × qty × dir_sign` where
 * `qty = pos_pct × NAV / entry`. Path B picked over Path A because the sign
 * is already correct and we avoid touching `entry_price` (some legacy rows
 * carry Decimal('NaN') entries — see project_regime_blended_sizer notes).
 *
 * Returns `null` (not 0) when any input is missing or NaN. Brand-new
 * positions (only one signal_pnl row, so `prev_pnl_pct` is null) and
 * NaN-poisoned `position_size_pct` rows fall through here so the dashboard's
 * em-dash fallback fires correctly instead of pretending a real $0 print.
 *
 * Caveat: this assumes the position was sized against today's NAV. An old
 * position sized when NAV was $80k vs today's $100k will be inflated by the
 * NAV ratio. Magnitude is small for short holds; documented in the task
 * report rather than corrected here.
 *
 * @param {object} args
 * @param {number|string|null} args.today_pnl_pct  Cumulative pnl_pct fraction today (signed)
 * @param {number|string|null} args.prev_pnl_pct   Cumulative pnl_pct fraction yesterday (signed)
 * @param {number|string|null} args.position_size_pct  Fraction of NAV (0.022 = 2.2%)
 * @param {number|string|null} args.nav            Account equity in USD
 * @returns {number|null}
 */
function computeDayPnlUsd({ today_pnl_pct, prev_pnl_pct, position_size_pct, nav }) {
  // `Number(null)` is 0 (a finite number), so we have to short-circuit on the
  // raw inputs before coercing — otherwise a missing NAV silently zeroes the
  // row and the em-dash fallback never fires.
  if (today_pnl_pct == null || prev_pnl_pct == null) return null;
  if (position_size_pct == null || nav == null)      return null;
  const today = Number(today_pnl_pct);
  const prev  = Number(prev_pnl_pct);
  const size  = Number(position_size_pct);
  const eq    = Number(nav);
  if (!Number.isFinite(today) || !Number.isFinite(prev)) return null;
  if (!Number.isFinite(size) || !Number.isFinite(eq))    return null;
  return (today - prev) * size * eq;
}

/**
 * Group positions by strategy_id.  Null/missing strategy_id collapses to '(unattributed)'.
 * Preserves input order within each group.  Adds subtotal_day_pnl_usd.
 *
 * Concept lifted from achannarasappa/ticker AssetGroup primitive — see
 * docs/archive/superpowers/plans/2026-05-15-fincept-imports-master-plan.md (1E).
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

module.exports = { groupByStrategy, computeDayPnlUsd };
