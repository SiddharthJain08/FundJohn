-- Migration 013: Add reported flag to signal_pnl for report trigger tracking

ALTER TABLE signal_pnl ADD COLUMN IF NOT EXISTS reported BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_signal_pnl_unreported
    ON signal_pnl(strategy_id, workspace_id) WHERE reported = FALSE;

-- signal_performance view for backward compatibility with cron-schedule references
CREATE OR REPLACE VIEW signal_performance AS
    SELECT
        id,
        signal_id,
        strategy_id,
        workspace_id,
        realized_pnl_pct   AS pnl_pct,
        reported,
        closed_at,
        close_reason,
        days_held,
        status
    FROM signal_pnl;
