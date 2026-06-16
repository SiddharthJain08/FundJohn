-- 137_research_candidate_kind_git.sql — allow kind='git' for Blueprint Fast Lane.
--
-- The git ingester (src/agent/curators/git_strategy_ingest.js) inserts cleanroom
-- git-imported strategies as research_candidates with kind='git'. The existing
-- research_candidates_kind_check (added in migration 050) only allows
-- ('paper','internal'), so every git insert would fail the check.
--
-- kind='git' is DELIBERATELY distinct from 'internal': _hunt's Population 1b
-- selects kind='internal' (paperhunter-bypass) with NO origin/gate check, so
-- tagging git rows 'internal' would let them be coded on any monolithic/recovery
-- saturday-brain run, bypassing the OPENCLAW_GIT_INGEST gate. Keeping 'git'
-- separate keeps git rows out of _hunt entirely — they are drained only by the
-- gated saturday_brain_finisher path. Additive + idempotent (DROP+ADD in a
-- DO-block, mirroring migration 050).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'research_candidates'::regclass
      AND conname  = 'research_candidates_kind_check'
  ) THEN
    ALTER TABLE research_candidates DROP CONSTRAINT research_candidates_kind_check;
  END IF;
  ALTER TABLE research_candidates
    ADD CONSTRAINT research_candidates_kind_check CHECK (kind IN ('paper', 'internal', 'git'));
END$$;
