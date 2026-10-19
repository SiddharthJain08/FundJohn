-- 165: RNG seed on both Monte-Carlo run tables (spec 2026-10-19 B3).
-- ADDITIVE ONLY: one nullable column per table, no backfill, no rewrite of existing rows.
-- Deploy ordering: requires migration 165; johnbot is restarted immediately after merge so `migrate()` applies it before the :3000 MC routes can INSERT.
-- Historical rows keep NULL (they were unseeded and cannot be reproduced).
ALTER TABLE strategy_regime_intraday_mc_runs
  ADD COLUMN IF NOT EXISTS seed BIGINT;

ALTER TABLE strategy_regime_mc_runs
  ADD COLUMN IF NOT EXISTS seed BIGINT;

COMMENT ON COLUMN strategy_regime_intraday_mc_runs.seed IS
  'RNG seed used for the run; derived from the request when not supplied (spec 2026-10-19 B3)';
COMMENT ON COLUMN strategy_regime_mc_runs.seed IS
  'RNG seed used for the run; derived from the request when not supplied (spec 2026-10-19 B3)';
