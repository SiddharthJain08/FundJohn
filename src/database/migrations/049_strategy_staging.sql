-- Migration 049: Strategy staging inbox
--
-- MasterMindJohn proposes strategies during chat; rows land here as
-- status='pending' awaiting user approval on the Research page. Approval
-- flips status to 'approved' and emits a downstream lifecycle event —
-- the actual strategy_registry row is created later once StrategyCoder
-- produces an implementation_path (that column is NOT NULL on registry).
-- Rejected proposals stay for audit so the same thesis isn't re-proposed.

CREATE TABLE IF NOT EXISTS strategy_staging (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  proposed_by        TEXT NOT NULL DEFAULT 'mastermind',
  source_session_id  UUID REFERENCES mastermind_chat_sessions(id) ON DELETE SET NULL,
  source_paper_id    TEXT,
  name               TEXT NOT NULL,
  thesis             TEXT,
  parameters         JSONB NOT NULL DEFAULT '{}'::jsonb,
  universe           TEXT[],
  signal_frequency   TEXT,
  regime_conditions  JSONB,
  status             TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','approved','rejected','promoted')),
  promoted_strategy_id TEXT REFERENCES strategy_registry(id) ON DELETE SET NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  decided_at         TIMESTAMPTZ,
  decided_by         TEXT,
  decision_note      TEXT
);

CREATE INDEX IF NOT EXISTS strategy_staging_status
  ON strategy_staging (status, created_at DESC);

CREATE INDEX IF NOT EXISTS strategy_staging_session
  ON strategy_staging (source_session_id)
  WHERE source_session_id IS NOT NULL;
