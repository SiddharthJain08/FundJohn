-- Add validation and backtest result columns to implementation_queue
-- for the auto-validation + auto-backtest gate pipeline.

ALTER TABLE implementation_queue
  ADD COLUMN IF NOT EXISTS error_log       TEXT,
  ADD COLUMN IF NOT EXISTS backtest_result JSONB;

-- Extended status values now in use:
--   pending          → waiting for StrategyCoder to pick up
--   coding           → StrategyCoder running
--   done             → code written; backtest pending
--   validation_failed → contract validation failed (before backtest)
--   backtest_failed  → contract OK but failed convergence gate
--   promoted         → passed gate, moved to PAPER in manifest
--   failed           → StrategyCoder subprocess error
