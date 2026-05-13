-- 087_strategy_memos_metadata.sql
-- Phase 2F per-memo audit trail: adds a metadata JSONB column to strategy_memos.
-- Used to record:
--   addenda_ids_active: [int]  — the calibration addenda that were prepended
--                                 to the Opus prompt for this memo's generation.
--   (extensible; future audit-relevant fields can land here without schema churn)
--
-- Append-only per CLAUDE.md invariant. ADD COLUMN with safe default for
-- backward compat.

ALTER TABLE strategy_memos
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_strategy_memos_addenda
    ON strategy_memos USING gin ((metadata -> 'addenda_ids_active'))
 WHERE metadata ? 'addenda_ids_active';
