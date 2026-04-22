-- Migration 046: backfill metadata for data-column queue
--
-- Phase 1 of the 10am-cycle pipeline restructure (plan:
-- /root/.claude/plans/i-want-to-now-flickering-cray.md).
--
-- The queue tables from migration 025 (data_ingestion_queue +
-- data_deprecation_queue) already handle add/remove lifecycle — this
-- migration adds the backfill window + execution status fields so the
-- daily orchestrator's queue_drain step can:
--   1. Pull APPROVED rows from data_ingestion_queue
--   2. Backfill the required historical window (for backtest) via the
--      column's provider-specific backfiller
--   3. Flip the row into backfill_status='complete' so subsequent daily
--      collections pick the column up
--   4. Symmetrically drain data_deprecation_queue APPROVED rows: drop the
--      column from the live collection set; historical data stays in
--      parquet for posterity.
--
-- Idempotent: uses ADD COLUMN IF NOT EXISTS so repeated boots don't error.

ALTER TABLE data_ingestion_queue
  ADD COLUMN IF NOT EXISTS strategy_id           TEXT REFERENCES strategy_registry(id),
  ADD COLUMN IF NOT EXISTS backfill_from         DATE,
  ADD COLUMN IF NOT EXISTS backfill_to           DATE,
  ADD COLUMN IF NOT EXISTS backfill_status       TEXT
    CHECK (backfill_status IN ('pending','running','complete','failed')),
  ADD COLUMN IF NOT EXISTS backfill_started_at   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS backfill_finished_at  TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS rows_backfilled       BIGINT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS backfill_error        TEXT;

-- Default new APPROVED rows to pending backfill — the drainer will pick
-- them up on the next cycle.
UPDATE data_ingestion_queue
   SET backfill_status = 'pending'
 WHERE status = 'APPROVED' AND backfill_status IS NULL;

CREATE INDEX IF NOT EXISTS data_ingestion_queue_drainable
  ON data_ingestion_queue (status, backfill_status)
  WHERE status = 'APPROVED' AND backfill_status IN ('pending','running','failed');

-- Data deprecation queue — track when the column actually got removed from
-- the live-collection set (schema_registry + data_columns).
ALTER TABLE data_deprecation_queue
  ADD COLUMN IF NOT EXISTS deletion_applied_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS deletion_error      TEXT;

CREATE INDEX IF NOT EXISTS data_deprecation_queue_drainable
  ON data_deprecation_queue (status, deletion_applied_at)
  WHERE status = 'APPROVED' AND deletion_applied_at IS NULL;
