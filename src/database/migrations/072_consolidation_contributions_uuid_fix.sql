-- 072_consolidation_contributions_uuid_fix.sql
-- Migration 069 declared consolidated_signal_id + contributing_signal_id as BIGINT
-- but execution_signals.id is UUID. Table is empty in dev (no consolidation
-- has actually written rows yet); safe to DROP and re-add with correct types.
-- Same fix pattern as migration 070 (which fixed parity_orders.contributing_signal_ids).

-- Drop the old PK first (depends on the BIGINT columns).
ALTER TABLE consolidation_contributions DROP CONSTRAINT IF EXISTS consolidation_contributions_pkey;

-- Drop the old strategy index too if present (won't hurt).
DROP INDEX IF EXISTS idx_consolidation_contrib_strategy;

-- Drop the typed columns and re-add as UUID.
ALTER TABLE consolidation_contributions DROP COLUMN IF EXISTS consolidated_signal_id;
ALTER TABLE consolidation_contributions DROP COLUMN IF EXISTS contributing_signal_id;
ALTER TABLE consolidation_contributions ADD COLUMN consolidated_signal_id UUID NOT NULL;
ALTER TABLE consolidation_contributions ADD COLUMN contributing_signal_id UUID NOT NULL;

-- Restore the primary key on the new types.
ALTER TABLE consolidation_contributions
  ADD CONSTRAINT consolidation_contributions_pkey
  PRIMARY KEY (consolidated_signal_id, contributing_signal_id);

-- Restore the strategy lookup index.
CREATE INDEX IF NOT EXISTS idx_consolidation_contrib_strategy
  ON consolidation_contributions (strategy_id, created_at DESC);
