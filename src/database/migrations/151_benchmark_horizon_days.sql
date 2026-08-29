-- 151: benchmark-relative sizing, amendment 1 (2026-08-29;
-- docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md D-A2/D-A6).
-- Seeds the sizer's benchmark horizon (trading days a synthetic SPY lot is held
-- when computing S_m). Grid 1,2,3,5,10,21 is cached by the sizer in
-- pipeline_config.benchmark_regime_sharpe (schema 2); this key picks the column.
-- Default 1 = the system's daily decision cadence (operator ruling 2026-08-29).
-- Idempotent: never overwrites an operator-edited value.
INSERT INTO pipeline_config (key, value, description, updated_at)
VALUES ('benchmark_horizon_days', '1',
        'Amendment 1 2026-08-29: horizon H (trading days) selecting the S_m column the sizer hurdle S_adj - S_m uses. Must be one of 1,2,3,5,10,21 (off-grid -> 1, logged). 1 = daily decision cadence.',
        NOW())
ON CONFLICT (key) DO NOTHING;

-- Migration 149's column comment described the retired contemporaneous rf=0
-- statistic and two gate readers that were removed on 2026-08-29.
COMMENT ON COLUMN strategy_backtest_regimes.benchmark_sharpe IS
  'Amendment 1 2026-08-29: SPY next-day (H=1) excess Sharpe (rf 5%) after closes tagged with this regime, over the run''s [start_date, end_date] window (src/backtest/benchmark_baseline.py regime_benchmark_sharpe). INFORMATIONAL only — no gate reads it since 2026-08-29. NULL = thin regime (<40 mark-days) or load failure.';
