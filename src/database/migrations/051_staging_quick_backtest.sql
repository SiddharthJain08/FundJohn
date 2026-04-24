-- 051_staging_quick_backtest.sql
-- Draft-time backtest results for internal strategies: populated asynchronously
-- by src/backtest/quick_backtest.py after MasterMindJohn inserts a staging row.

ALTER TABLE strategy_staging ADD COLUMN IF NOT EXISTS quick_backtest_json JSONB;
ALTER TABLE strategy_staging ADD COLUMN IF NOT EXISTS quick_backtest_started_at TIMESTAMPTZ;
ALTER TABLE strategy_staging ADD COLUMN IF NOT EXISTS quick_backtest_error TEXT;

CREATE INDEX IF NOT EXISTS idx_staging_backtest_pending
  ON strategy_staging((quick_backtest_json IS NULL))
  WHERE status = 'pending' AND quick_backtest_json IS NULL;
