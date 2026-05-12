-- 071_parity_orders_production_source.sql
-- Task 17: Add 'production' to the parity_orders source check constraint
-- so trade_parity_capture can mirror alpaca_submissions with source='production'.
-- Also brings the constraint in sync with parity_diff.py which already expects
-- source IN ('regime_blended', 'deterministic', 'production').

ALTER TABLE parity_orders DROP CONSTRAINT parity_orders_source_check;
ALTER TABLE parity_orders ADD CONSTRAINT parity_orders_source_check
  CHECK (source IN ('regime_blended', 'deterministic', 'production'));
