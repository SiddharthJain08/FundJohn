-- 119_alpaca_submissions_qty_numeric.sql
-- SP-3.1 Phase D: widen alpaca_submissions.qty INT → NUMERIC so fractional
-- crypto order quantities (e.g. 0.00018 BTC) survive the audit write instead
-- of truncating to 0. Additive widening; equity integer values cast losslessly.
-- Append-only-safe: no column/row drop (CLAUDE.md NEVER-DELETE invariant).
ALTER TABLE alpaca_submissions
  ALTER COLUMN qty TYPE NUMERIC USING qty::numeric;
