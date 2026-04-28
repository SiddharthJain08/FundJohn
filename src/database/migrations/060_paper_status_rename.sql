-- 060_paper_status_rename.sql
--
-- One-shot, idempotent data migration for the fused-staging-approval rewrite
-- (2026-04-27). Under the new lifecycle, the `paper` state is a frozen
-- legacy: every existing PAPER strategy is the equivalent of the new
-- CANDIDATE state ("backtest metrics on the registry, awaiting operator's
-- live click"). This migration renames the registry status; the manifest
-- rewrite is in scripts/migrate_paper_to_candidate.py (run separately —
-- it needs to acquire the manifest cross-process lock).
--
-- Idempotent: WHERE filter only matches rows that haven't been migrated.

UPDATE strategy_registry
   SET status = 'pending_approval'
 WHERE status = 'paper';
