-- Migration 010: Security infrastructure
-- skill_audit: vetting log per skill
-- pipeline_runs: verification_status column

CREATE TABLE IF NOT EXISTS skill_audit (
  skill_name       TEXT PRIMARY KEY,
  vetted_at        TIMESTAMPTZ DEFAULT NOW(),
  approved         BOOLEAN NOT NULL,
  violations_json  JSONB DEFAULT '[]',
  vetted_by        TEXT DEFAULT 'skill-vetter'
);

CREATE INDEX IF NOT EXISTS idx_skill_audit_approved ON skill_audit(approved);

-- Add verification_status to pipeline_runs if not already present
ALTER TABLE pipeline_runs
  ADD COLUMN IF NOT EXISTS verification_status TEXT DEFAULT 'SKIPPED'
    CHECK (verification_status IN ('VERIFIED','UNVERIFIED','SKIPPED'));
