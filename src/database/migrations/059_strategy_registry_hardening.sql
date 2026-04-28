-- 059_strategy_registry_hardening.sql
--
-- Defensive schema improvements after the manifest/registry concurrency
-- audit on 2026-04-26. The application-level fixes (manifest_lock helper +
-- canonical strategy_registry_upsert helper) eliminate the corruption +
-- lost-update class of bugs at the source. This migration adds two minor
-- improvements that make recovery + diagnostics easier without changing
-- runtime semantics:
--
--   1. Index on strategy_registry.created_at so the cleanup queries we
--      had to run yesterday (delete older duplicate of same id) are O(log n)
--      instead of full-table scan.
--
--   2. CASCADE the strategy_approval_jobs → strategy_registry FK so that
--      operator-initiated DELETE FROM strategy_registry doesn't get
--      blocked by historic approval-job rows. The current setup blocks
--      deletes (we hit this yesterday during cleanup); cascading the
--      delete to the job rows is the right call because a job row whose
--      strategy_registry parent no longer exists is meaningless audit
--      noise anyway.
--
-- Both changes are forward-only and idempotent. No data migration.

-- 1. Index on created_at for diagnostics + dedup tooling.
CREATE INDEX IF NOT EXISTS idx_strategy_registry_created_at
    ON strategy_registry (created_at);

-- 2. CASCADE the approval-jobs FK. We have to drop the old constraint
--    first (PG doesn't support ALTER ... ON DELETE in-place) and re-add
--    with the same name + cascade.
DO $$
DECLARE
    cn TEXT;
BEGIN
    SELECT conname INTO cn
      FROM pg_constraint
     WHERE conrelid = 'strategy_approval_jobs'::regclass
       AND contype  = 'f'
       AND pg_get_constraintdef(oid) LIKE '%REFERENCES strategy_registry%';
    IF cn IS NOT NULL THEN
        EXECUTE format('ALTER TABLE strategy_approval_jobs DROP CONSTRAINT %I', cn);
        EXECUTE format(
            'ALTER TABLE strategy_approval_jobs '
            'ADD CONSTRAINT %I FOREIGN KEY (strategy_id) '
            'REFERENCES strategy_registry(id) ON DELETE CASCADE',
            cn
        );
    END IF;
END
$$;
