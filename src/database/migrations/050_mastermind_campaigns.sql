-- 050_mastermind_campaigns.sql
-- Campaigns for MasterMindJohn-driven broad-scale strategy research.
-- Also adds kind='internal' support so knowledge-drafted candidates skip PaperHunter.

-- 1. Tag candidates with campaign lineage (kind column already exists; add CHECK + campaign_id)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'research_candidates'::regclass AND conname = 'research_candidates_kind_check'
  ) THEN
    ALTER TABLE research_candidates
      ADD CONSTRAINT research_candidates_kind_check CHECK (kind IN ('paper', 'internal'));
  END IF;
END$$;

ALTER TABLE research_candidates ADD COLUMN IF NOT EXISTS campaign_id UUID;
CREATE INDEX IF NOT EXISTS idx_research_candidates_kind ON research_candidates(kind);
CREATE INDEX IF NOT EXISTS idx_research_candidates_campaign ON research_candidates(campaign_id)
  WHERE campaign_id IS NOT NULL;

-- 2. Campaigns driven by MasterMindJohn
CREATE TABLE IF NOT EXISTS research_campaigns (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID REFERENCES mastermind_chat_sessions(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  request_text TEXT NOT NULL,
  plan_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'planning'
    CHECK (status IN ('planning','awaiting_ack','running','completed','cancelled','failed')),
  progress_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_research_campaigns_session ON research_campaigns(session_id)
  WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_research_campaigns_active ON research_campaigns(status, created_at DESC)
  WHERE status IN ('running','awaiting_ack','planning');

-- 3. FK back to campaigns (after table exists)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_candidate_campaign'
  ) THEN
    ALTER TABLE research_candidates
      ADD CONSTRAINT fk_candidate_campaign FOREIGN KEY (campaign_id)
      REFERENCES research_campaigns(id) ON DELETE SET NULL;
  END IF;
END$$;
