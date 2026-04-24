-- 052_comprehensive_memos.sql
-- Replaces the legacy mastermind_weekly_reports table with a structured
-- triple: strategy_memos (comprehensive lifetime review per strategy),
-- strategy_sizing_recommendations (exact sizing deltas derived from memos), and
-- paper_source_expansions (weekly Opus-steered paper ingestion log).

-- 1. Drop legacy table — user has confirmed these memos were scaffolding.
DROP TABLE IF EXISTS mastermind_weekly_reports CASCADE;

-- 2. Comprehensive strategy memos — one row per strategy per weekly review.
CREATE TABLE IF NOT EXISTS strategy_memos (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL REFERENCES strategy_registry(id) ON DELETE CASCADE,
  memo_date DATE NOT NULL DEFAULT CURRENT_DATE,
  lifetime_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  parameter_analysis JSONB NOT NULL DEFAULT '{}'::jsonb,
  recommendations JSONB NOT NULL DEFAULT '{}'::jsonb,
  markdown_body TEXT NOT NULL,
  cost_usd NUMERIC,
  posted_to_discord BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strategy_memos_strategy_date
  ON strategy_memos (strategy_id, memo_date DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_memos_date
  ON strategy_memos (memo_date DESC);

-- 3. Position recommendations — exact sizing + bracket deltas per strategy.
CREATE TABLE IF NOT EXISTS strategy_sizing_recommendations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  strategy_id TEXT NOT NULL REFERENCES strategy_registry(id) ON DELETE CASCADE,
  memo_id UUID REFERENCES strategy_memos(id) ON DELETE SET NULL,
  rec_date DATE NOT NULL DEFAULT CURRENT_DATE,
  current_size_pct NUMERIC,
  recommended_size_pct NUMERIC NOT NULL,
  size_delta_pct NUMERIC,
  stop_delta_pct NUMERIC,
  target_delta_pct NUMERIC,
  hold_days_delta INT,
  reasoning TEXT NOT NULL,
  action_taken TEXT NOT NULL DEFAULT 'pending'
    CHECK (action_taken IN ('pending','applied','ignored','superseded')),
  posted_to_discord BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strategy_sizing_recs_strategy_date
  ON strategy_sizing_recommendations (strategy_id, rec_date DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_sizing_recs_date
  ON strategy_sizing_recommendations (rec_date DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_sizing_recs_pending
  ON strategy_sizing_recommendations (rec_date DESC)
  WHERE action_taken = 'pending';

-- 4. Paper-source expansion log — one row per Sunday run.
CREATE TABLE IF NOT EXISTS paper_source_expansions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_date DATE NOT NULL DEFAULT CURRENT_DATE,
  status TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running','completed','failed','partial')),
  queries_used TEXT[],
  sources_discovered JSONB DEFAULT '[]'::jsonb,
  papers_imported INT NOT NULL DEFAULT 0,
  papers_skipped_dup INT NOT NULL DEFAULT 0,
  cost_usd NUMERIC,
  duration_seconds INT,
  error_detail TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_paper_expansions_date
  ON paper_source_expansions (run_date DESC);
