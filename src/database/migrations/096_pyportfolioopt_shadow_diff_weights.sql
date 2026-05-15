-- Phase 1G fix: add weight-space diff alongside dollar-diff in the shadow-sizer
-- capture table.  Equity-independent and apples-to-apples even when handoff equity
-- (placeholder $1M) differs from live sizer's actual equity (~$100k).
ALTER TABLE pyportfolioopt_shadow_runs
    ADD COLUMN IF NOT EXISTS diff_weights JSONB;
