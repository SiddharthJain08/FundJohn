-- 148: advisory sleeve tail statistics (task P3+R3, five-repo-adoptions,
-- 2026-08-24). Adds a per-regime CVaR(5%) and a distinctly-named per-trade
-- Sortino computed by src/backtest/tail_stats.py (pure numpy, population
-- downside-deviation form: sortino = mean(r) / sqrt(mean(min(r,0)^2))).
--
-- NOTE on naming: strategy_backtest_regimes.sortino ALREADY EXISTS (migration
-- 135_backtest_sortino_calmar.sql) and is populated by aggregate_metrics() /
-- aggregate_per_regime() in unified_backtest.py from an ANNUALIZED Sortino
-- derived off the equal-weight DAILY PORTFOLIO equity curve -- a different
-- metric with a different formula, already read by the dashboard
-- (src/channels/api/server.js, `br.sortino`). Task P3+R3's wiring sketch
-- assumed the column did not exist yet ("ADD COLUMN IF NOT EXISTS sortino");
-- it does, under a different definition. Overwriting it in place would leave
-- pre-148 rows and post-148 rows silently holding numbers computed two
-- different ways under one column name, in a live production table. Per the
-- task's own binding constraint ("All new persistence is nullable columns"),
-- this migration instead adds `tail_sortino` as its own column and leaves
-- `sortino` untouched. Both columns are advisory-only (grep confirms no
-- gate/sizing/promotion reader consumes either `sortino` or a hypothetical
-- `tail_sortino` from this table -- promotion_service.js._regimeSleeves only
-- reads sharpe/trade_count/max_dd_pct/calmar). If the operator wants the
-- literal overwrite instead, it is a one-line change: rename the INSERT
-- target from tail_sortino back to sortino in unified_backtest.py.
ALTER TABLE strategy_backtest_regimes
  ADD COLUMN IF NOT EXISTS cvar_5       NUMERIC,
  ADD COLUMN IF NOT EXISTS tail_sortino NUMERIC;

COMMENT ON COLUMN strategy_backtest_regimes.cvar_5 IS
  'advisory tail stats, 2026-08-24 five-repo P3; never a gate input. Mean of the worst floor(0.05*n) per-trade pnl_pct observations in this regime sleeve (src/backtest/tail_stats.py sleeve_tail_stats); NULL below min_obs or when the 5% tail slice is empty.';

COMMENT ON COLUMN strategy_backtest_regimes.tail_sortino IS
  'advisory tail stats, 2026-08-24 five-repo P3; never a gate input. Per-trade Sortino = mean(pnl_pct) / population downside-deviation(pnl_pct) (src/backtest/tail_stats.py sleeve_tail_stats). Distinct from the pre-existing `sortino` column (migration 135), which is an annualized Sortino off the daily portfolio equity curve -- deliberately NOT overwritten; see file header.';
