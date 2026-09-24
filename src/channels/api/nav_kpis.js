'use strict';
/**
 * nav_kpis.js — live trading KPIs for the portfolio page (QD spec §5 E5).
 *
 * Pure functions + two read-only SQL strings, kept out of server.js so they
 * can be unit-tested without a database or an HTTP server.
 *
 * SCOPING IS LOAD-BEARING. Both queries repeat the exact three predicates the
 * existing summary aggregates use (server.js:1224-1246):
 *   sp.status = 'closed'
 *   sp.close_reason IS DISTINCT FROM 'rolled_continuation'   (SP-6 D1 roll
 *       segments are segments of an ongoing position, not trades)
 *   es.signal_date >= $1::date                               (account epoch)
 * Drop any one of them and the new tiles disagree with the tiles beside them.
 *
 * MAE NOTE: there is no live per-signal intraday high/low store —
 * `trade_daily_marks` is a backtest-side FUNCTION
 * (src/backtest/backtest_panel.py:106), not a table. The live MAE here is
 * therefore CLOSE-TO-CLOSE: the worst daily mark a signal ever printed,
 * floored at 0. The tile says so.
 */

// One row. gross_loss is returned POSITIVE (negated in SQL) so the JS never
// has to reason about the sign of a sum of negatives.
const TRADE_KPI_SQL = `
  SELECT COUNT(*)                                                                   AS n_closed,
         COALESCE( SUM(sp.realized_pnl_pct) FILTER (WHERE sp.realized_pnl_pct > 0), 0) AS gross_win,
         COALESCE(-SUM(sp.realized_pnl_pct) FILTER (WHERE sp.realized_pnl_pct < 0), 0) AS gross_loss,
                   AVG(sp.realized_pnl_pct) FILTER (WHERE sp.realized_pnl_pct > 0)     AS avg_win,
                  -AVG(sp.realized_pnl_pct) FILTER (WHERE sp.realized_pnl_pct < 0)     AS avg_loss,
                   AVG(sp.realized_pnl_pct)                                            AS expectancy_pct
    FROM signal_pnl sp
    JOIN execution_signals es ON es.id = sp.signal_id
   WHERE sp.status = 'closed'
     AND sp.realized_pnl_pct IS NOT NULL
     AND sp.close_reason IS DISTINCT FROM 'rolled_continuation'
     AND es.signal_date >= $1::date
`;

// Per-signal worst daily mark (floored at 0 — a trade that never went under
// water has an MAE of zero, not a positive number), then the median.
const MAE_SQL = `
  WITH closed AS (
    SELECT DISTINCT sp.signal_id
      FROM signal_pnl sp
      JOIN execution_signals es ON es.id = sp.signal_id
     WHERE sp.status = 'closed'
       AND sp.realized_pnl_pct IS NOT NULL
       AND sp.close_reason IS DISTINCT FROM 'rolled_continuation'
       AND es.signal_date >= $1::date
  ),
  marks AS (
    SELECT sp.signal_id, LEAST(MIN(sp.unrealized_pnl_pct), 0) AS mae_pct
      FROM signal_pnl sp
      JOIN closed c ON c.signal_id = sp.signal_id
     WHERE sp.unrealized_pnl_pct IS NOT NULL
       -- Direct bound on the marks side (review, 2026-09-23): a signal's mark
       -- history postdates its signal_date, so this changes nothing semantically
       -- but stops a cost-based plan from ever seq-scanning the whole
       -- append-only signal_pnl history (idx_signal_pnl_date covers it).
       AND sp.pnl_date >= $1::date
     GROUP BY sp.signal_id
  )
  SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY mae_pct)::numeric, 4) AS mae_median_pct,
         COUNT(*)::int                                                           AS mae_n
    FROM marks
`;

function _num(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * @param {object} row one TRADE_KPI_SQL row
 * @returns {{profit_factor:number|null, expectancy_pct:number|null,
 *            payoff_ratio:number|null, avg_win_pct:number|null,
 *            avg_loss_pct:number|null, gross_win_pct:number|null,
 *            gross_loss_pct:number|null}}
 */
function deriveTradeKpis(row) {
  const r         = row || {};
  const n         = _num(r.n_closed) || 0;
  const grossWin  = _num(r.gross_win);
  const grossLoss = _num(r.gross_loss);
  const avgWin    = _num(r.avg_win);
  const avgLoss   = _num(r.avg_loss);
  // expectancy == AVG(realized_pnl_pct) over exactly the rows `avg_realized`
  // already averages. Same number, surfaced under the name an operator looks
  // for; not a separately weighted statistic.
  const expectancy = _num(r.expectancy_pct);

  const round4 = (v) => (v === null ? null : Math.round(v * 1e4) / 1e4);

  return {
    profit_factor: (n > 0 && grossLoss !== null && grossLoss > 0 && grossWin !== null)
      ? Math.round((grossWin / grossLoss) * 1e4) / 1e4 : null,
    payoff_ratio: (avgWin !== null && avgLoss !== null && avgLoss > 0)
      ? Math.round((avgWin / avgLoss) * 1e4) / 1e4 : null,
    expectancy_pct: n > 0 ? round4(expectancy) : null,
    avg_win_pct:    n > 0 ? round4(avgWin)  : null,
    avg_loss_pct:   n > 0 ? round4(avgLoss) : null,
    gross_win_pct:  n > 0 ? round4(grossWin)  : null,
    gross_loss_pct: n > 0 ? round4(grossLoss) : null,
  };
}

/**
 * Month-over-month NAV return from the OHLC store's closes.
 * Anchor = the previous CALENDAR month's last close (not the month's own
 * first open), so a gap over a month boundary is attributed to the new month.
 * The first month in the series has no anchor and returns null.
 */
function navMonthlyReturns(days, { limit = 12 } = {}) {
  if (!days || typeof days !== 'object') return [];
  const dates = Object.keys(days).filter(d => days[d] && days[d].close != null).sort();
  if (!dates.length) return [];

  const lastCloseByMonth = new Map();   // 'YYYY-MM' -> close of its last session
  const countByMonth     = new Map();
  for (const d of dates) {
    const m = d.slice(0, 7);
    lastCloseByMonth.set(m, Number(days[d].close));
    countByMonth.set(m, (countByMonth.get(m) || 0) + 1);
  }

  const months = [...lastCloseByMonth.keys()].sort();
  const rows = months.map((m, i) => {
    const prev = i > 0 ? lastCloseByMonth.get(months[i - 1]) : null;
    const cur  = lastCloseByMonth.get(m);
    const ret  = (prev != null && prev > 0 && Number.isFinite(cur))
      ? (cur / prev - 1) * 100 : null;
    return { month: m, return_pct: ret, days: countByMonth.get(m) || 0 };
  });
  return rows.slice(-Math.max(1, limit));
}

/** Session-over-session up / down / flat day counts from the same store. */
function navDayCounts(days) {
  const out = { win_days: 0, lose_days: 0, flat_days: 0 };
  if (!days || typeof days !== 'object') return out;
  const dates = Object.keys(days).filter(d => days[d] && days[d].close != null).sort();
  let prev = null;
  for (const d of dates) {
    const c = Number(days[d].close);
    if (prev != null && Number.isFinite(c)) {
      if (c > prev) out.win_days++;
      else if (c < prev) out.lose_days++;
      else out.flat_days++;
    }
    prev = Number.isFinite(c) ? c : prev;
  }
  return out;
}

module.exports = { TRADE_KPI_SQL, MAE_SQL, deriveTradeKpis, navMonthlyReturns, navDayCounts };
