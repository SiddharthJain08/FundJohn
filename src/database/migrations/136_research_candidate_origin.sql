-- 136_research_candidate_origin.sql — tag a candidate's source lane (Blueprint Fast Lane).
-- Additive. Default 'paper' keeps every existing row + the academic lane byte-identical.
ALTER TABLE research_candidates
  ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'paper'
    CHECK (origin IN ('paper','git_blueprint','blog_blueprint'));
ALTER TABLE research_candidates
  ADD COLUMN IF NOT EXISTS reference_url TEXT;
CREATE INDEX IF NOT EXISTS idx_research_candidates_origin ON research_candidates(origin);
