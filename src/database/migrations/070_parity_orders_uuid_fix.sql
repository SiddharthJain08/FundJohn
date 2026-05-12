-- 070_parity_orders_uuid_fix.sql
-- Migration 069 declared parity_orders.contributing_signal_ids as BIGINT[]
-- but execution_signals.id is UUID. parity_orders is empty in dev (parity
-- writes haven't started); safe to drop and re-add the column with the
-- correct type.

ALTER TABLE parity_orders DROP COLUMN IF EXISTS contributing_signal_ids;
ALTER TABLE parity_orders ADD COLUMN contributing_signal_ids UUID[];
