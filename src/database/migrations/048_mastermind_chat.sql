-- Migration 048: MasterMindJohn interactive chat sessions
--
-- Backs the Research page chatbox. Each session wraps a persistent
-- claude-bin conversation (resumed via --session-id). Every user turn,
-- assistant turn, tool_use, and tool_result stream-json event is
-- persisted as one row in mastermind_chat_messages. Messages can
-- optionally link back to a paper (research_candidates) or a strategy
-- (strategy_registry) so cross-session recall can join across them.

CREATE TABLE IF NOT EXISTS mastermind_chat_sessions (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title               TEXT,
  claude_session_id   TEXT UNIQUE,                  -- value passed as --session-id to claude-bin
  status              TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','archived')),
  started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_active_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_snapshot_at    TIMESTAMPTZ,                  -- when context preamble was last refreshed
  total_cost_usd      NUMERIC NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS mastermind_chat_sessions_active
  ON mastermind_chat_sessions (last_active_at DESC)
  WHERE status = 'active';

CREATE TABLE IF NOT EXISTS mastermind_chat_messages (
  id            BIGSERIAL PRIMARY KEY,
  session_id    UUID NOT NULL REFERENCES mastermind_chat_sessions(id) ON DELETE CASCADE,
  role          TEXT NOT NULL
                 CHECK (role IN ('user','assistant','tool_use','tool_result','system')),
  content       JSONB NOT NULL,          -- text OR structured stream-json event
  paper_id      TEXT,                    -- optional FK to research_candidates.paper_id
  strategy_id   TEXT REFERENCES strategy_registry(id) ON DELETE SET NULL,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  cost_usd      NUMERIC,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mastermind_chat_messages_session_time
  ON mastermind_chat_messages (session_id, created_at);

CREATE INDEX IF NOT EXISTS mastermind_chat_messages_paper
  ON mastermind_chat_messages (paper_id)
  WHERE paper_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS mastermind_chat_messages_strategy
  ON mastermind_chat_messages (strategy_id)
  WHERE strategy_id IS NOT NULL;
