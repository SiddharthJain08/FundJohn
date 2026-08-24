-- 149: benchmark-relative promotion criterion (task R1, five-repo-adoptions,
-- 2026-08-24). Adds a per-regime-sleeve benchmark Sharpe so the candidate->
-- live promotion gate can require a sleeve to beat the regime-conditioned
-- SPY baseline by MIN_EXCESS_SHARPE_VS_BENCHMARK (0.0 default; see
-- src/strategies/lifecycle.py MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS),
-- not merely clear the flat positive-Sharpe floor.
--
-- Computed by src/backtest/benchmark_baseline.py::regime_benchmark_sharpe
-- (annualized Sharpe of the benchmark's close-to-close daily returns,
-- computed separately per regime over the backtest's own [start_date,
-- end_date] window) and written by src/backtest/unified_backtest.py's
-- persist path alongside each strategy_backtest_regimes row. NULL means
-- "no benchmark data for this regime/window" (thin regime, load failure,
-- or a pre-R1 row) -- both regime_qualification.qualifies_regime (python)
-- and promotion_service.js judgeRegimeSleeve (JS) treat NULL as
-- "criterion skipped, fail open" (logged), never as a gate failure.
--
-- Additive, nullable, idempotent -- matches migration 147/148's style.
ALTER TABLE strategy_backtest_regimes
  ADD COLUMN IF NOT EXISTS benchmark_sharpe NUMERIC;

COMMENT ON COLUMN strategy_backtest_regimes.benchmark_sharpe IS
  'R1 2026-08-24: annualized Sharpe (rf=0) of the SPY benchmark''s close-to-close returns over this regime''s tagged days within the run''s [start_date, end_date] window (src/backtest/benchmark_baseline.py regime_benchmark_sharpe). NULL = no benchmark data (thin regime <40 tagged days, load failure, or pre-R1 row) -- the excess-Sharpe-vs-benchmark gate leg is skipped (fail-open), never blocked, when NULL. Read by regime_qualification.qualifies_regime and promotion_service.js judgeRegimeSleeve.';
