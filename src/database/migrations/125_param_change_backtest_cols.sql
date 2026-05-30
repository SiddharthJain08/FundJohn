-- 125: backtest justification columns on the regime-param audit log.
-- Carries the baseline/candidate Sharpe + trade count that justified a
-- stop/target change applied by the Saturday backtest-coupling step
-- (source='saturday_coupling'). Additive, nullable — existing rows + callers
-- are unaffected.
ALTER TABLE strategy_regime_param_changes
    ADD COLUMN IF NOT EXISTS bt_sharpe_before NUMERIC,
    ADD COLUMN IF NOT EXISTS bt_sharpe_after  NUMERIC,
    ADD COLUMN IF NOT EXISTS bt_n_trades      INT;
