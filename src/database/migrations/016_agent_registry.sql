-- Migration 016: Agent Registry
-- Tracks all OpenClaw agents, their channels, webhook URLs, and live status.

CREATE TABLE IF NOT EXISTS agent_registry (
  id             TEXT PRIMARY KEY,
  display_name   TEXT NOT NULL,
  emoji          TEXT NOT NULL DEFAULT '🤖',
  model          TEXT,
  description    TEXT,
  channel_keys   TEXT[]  DEFAULT '{}',
  status         TEXT NOT NULL DEFAULT 'offline',  -- online | offline | busy | idle
  current_task   TEXT,
  last_seen_at   TIMESTAMPTZ,
  webhook_urls   JSONB DEFAULT '{}'::jsonb,
  created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Seed the five core agents (idempotent)
INSERT INTO agent_registry (id, display_name, emoji, model, description, channel_keys)
VALUES
  ('botjohn',      '🦞 BotJohn',     '🦞', 'claude-opus-4-6',              'Portfolio Manager & Orchestrator', ARRAY['general','botjohn-log']),
  ('databot',      '📡 DataBot',     '📡', 'claude-haiku-4-5-20251001',    'Data Pipeline & Collection',       ARRAY['pipeline-feed','data-alerts']),
  ('researchdesk', '🔬 ResearchDesk','🔬', 'claude-sonnet-4-6',            'Equity Research & Diligence',      ARRAY['research-feed','diligence-memos']),
  ('tradedesk',    '📈 TradeDesk',   '📈', 'claude-sonnet-4-6',            'Trade Execution & Risk',           ARRAY['trade-signals','trade-reports']),
  ('alertbot',     '🚨 AlertBot',    '🚨', NULL,                            'Risk Alerts & Kill Signals',       ARRAY['alerts'])
ON CONFLICT (id) DO UPDATE
  SET display_name = EXCLUDED.display_name,
      emoji        = EXCLUDED.emoji,
      model        = EXCLUDED.model,
      description  = EXCLUDED.description,
      channel_keys = EXCLUDED.channel_keys;
