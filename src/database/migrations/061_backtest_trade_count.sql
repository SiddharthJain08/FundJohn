-- 061_backtest_trade_count.sql
--
-- Add backtest_trade_count to strategy_registry. The convergence backtest
-- (auto_backtest.py) already returns trade_count alongside sharpe/dd/return,
-- but the registry was discarding it. Surfacing it as a first-class column
-- lets the dashboard distinguish "0 trades — strategy emitted no signals"
-- from "metrics unknown — never backtested" (the previous behavior used
-- NULL for both, which collapsed those cases).
--
-- Idempotent.

ALTER TABLE strategy_registry
  ADD COLUMN IF NOT EXISTS backtest_trade_count INT;
