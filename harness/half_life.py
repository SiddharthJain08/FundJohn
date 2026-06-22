# harness/half_life.py
"""Per-strategy half_life proxy for the Ensemble Exit Policy harness.

`autocorr_half_life` is the verbatim spec estimator (AR(1) lag-1 autocorr ->
half-life, clamped). `series_for_strategy`/`half_life_for` source the real
per-strategy daily-return series read-only from production tables.

Schema adaptations vs the plan literal (real-schema-forced; see notes):
  * `strategy_backtest_trades` has NO `id` column (PK = run_id, trade_seq) and
    holds MULTIPLE backtest runs per strategy (4-7 overlapping 2016-2026 runs).
    Concatenating runs corrupts the lag-1 autocorrelation across run boundaries,
    so we filter to the single LATEST run (max(entry_date), run_id tiebreak for
    determinism) and order chronologically by (entry_date, trade_seq).
  * The half_life is consumed in TRADING-DAY bars (plan: 1 bar = 1 trading day;
    design line 44: "AR(1) fit on ... daily returns"). The table is per-trade
    (~dozens of trades/day), so we aggregate to ONE observation per trading day
    via equal-weight mean of pnl_pct (no per-trade weight column exists). This
    also keeps the read small (~2k daily rows vs 100k+ per-trade rows).
  * `signal_pnl` fallback is aggregated the same way by `closed_at`.
"""
import numpy as np


def autocorr_half_life(returns, lo=1.0, hi=252.0):
    r = np.asarray(returns, float); r = r[np.isfinite(r)]
    if r.size < 8:
        return float("nan")
    r = r - r.mean()
    denom = float(np.dot(r, r))
    if denom <= 0:
        return float(lo)
    rho = float(np.dot(r[:-1], r[1:]) / denom)
    if rho <= 0:
        return float(lo)
    if rho >= 1:
        return float(hi)
    return float(min(max(np.log(2.0) / (-np.log(rho)), lo), hi))


def series_for_strategy(strategy_id, conn):
    """Daily-aggregated return series for a strategy (read-only).

    Prefers strategy_backtest_trades.pnl_pct (latest run, mean-per-day);
    falls back to signal_pnl.realized_pnl_pct (mean-per-day) if < 8 daily obs.
    Returns an empty float array when neither source has data.
    """
    cur = conn.cursor()
    cur.execute(
        """select entry_date, avg(pnl_pct)
             from strategy_backtest_trades
            where strategy_id=%s and pnl_pct is not null
              and run_id = (select run_id from strategy_backtest_trades
                              where strategy_id=%s
                           group by run_id
                           order by max(entry_date) desc, run_id desc
                              limit 1)
         group by entry_date
         order by entry_date""",
        (strategy_id, strategy_id),
    )
    rows = [float(x[1]) for x in cur.fetchall() if x[1] is not None]
    if len(rows) < 8:
        cur.execute(
            """select closed_at, avg(realized_pnl_pct)
                 from signal_pnl
                where strategy_id=%s and realized_pnl_pct is not null
                  and closed_at is not null
             group by closed_at
             order by closed_at""",
            (strategy_id,),
        )
        rows = [float(x[1]) for x in cur.fetchall() if x[1] is not None]
    return np.array(rows, float)


def half_life_for(strategy_id, conn, mode, cadence_days):
    if mode == "cadence":
        return float(cadence_days)
    est = autocorr_half_life(series_for_strategy(strategy_id, conn))
    return float(cadence_days) if not np.isfinite(est) else est
