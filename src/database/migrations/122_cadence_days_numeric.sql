-- 122_cadence_days_numeric.sql
-- Widen strategy_weights_by_regime.cadence_days from INTEGER to NUMERIC(10,4)
-- so the producer can persist the EXACT backtest avg_holding_days (e.g. 7.67)
-- instead of rounding/ceiling to the nearest integer day.
--
-- Operator directive 2026-05-29: cadence is sourced from the unified_backtest's
-- measured holding period, with no rounding. A strategy whose backtest holds
-- 4.71 days gets sized as 4.71 days, not 5.
--
-- Append-only widen — existing integer rows convert losslessly. Sizers
-- already accept float math (daily_weight = w / sqrt(cadence_days);
-- regime_blended_sizer's age-vs-cadence comparison works with floats).

ALTER TABLE strategy_weights_by_regime
    ALTER COLUMN cadence_days TYPE NUMERIC(10,4);
