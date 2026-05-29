-- 122_alpaca_submissions_instrument_class.sql
-- SP-5.1a: tag submission rows by instrument_class for audit slicing.
-- Additive only; no DROP/DELETE. NULL = pre-migration / legacy 'equity'.
-- Master-data invariant: column ADD is explicitly allowed.
ALTER TABLE alpaca_submissions
  ADD COLUMN IF NOT EXISTS instrument_class TEXT NULL;
