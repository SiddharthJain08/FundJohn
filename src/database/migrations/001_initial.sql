-- OpenClaw v2 Initial Schema

-- Workspaces
CREATE TABLE IF NOT EXISTS workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    sandbox_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'
);

-- Threads (conversations within a workspace)
CREATE TABLE IF NOT EXISTS threads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    channel_id TEXT,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    last_active TIMESTAMPTZ DEFAULT NOW(),
    summary TEXT,
    metadata JSONB DEFAULT '{}'
);

-- Subagent checkpoints (for resume)
CREATE TABLE IF NOT EXISTS checkpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id UUID REFERENCES threads(id) ON DELETE CASCADE,
    subagent_type TEXT NOT NULL,
    ticker TEXT,
    state JSONB NOT NULL,
    status TEXT DEFAULT 'running',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- Analysis results (queryable research)
CREATE TABLE IF NOT EXISTS analyses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    thread_id UUID REFERENCES threads(id),
    ticker TEXT NOT NULL,
    analysis_type TEXT NOT NULL,
    verdict TEXT,
    signals TEXT[],
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    stale_after TIMESTAMPTZ
);

-- Verdict cache (fast historical queries, cross-name pattern detection)
CREATE TABLE IF NOT EXISTS verdict_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    analysis_date DATE NOT NULL,
    analysis_type TEXT NOT NULL,            -- 'diligence' | 'trade'
    verdict TEXT NOT NULL,                  -- 'PROCEED' | 'REVIEW' | 'KILL'
    checklist JSONB,                        -- per-item PASS/FAIL results
    score TEXT,                             -- '6/6', '4/6', etc.
    signals TEXT[],                         -- kill signals, warnings
    bull_target NUMERIC,
    bear_target NUMERIC,
    ev_pct NUMERIC,
    position_size_pct NUMERIC,
    risk_verdict TEXT,                      -- 'APPROVED' | 'REDUCED' | 'BLOCKED' | 'PENDING_REVIEW'
    memo_path TEXT,
    stale_after TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Trade recommendations
CREATE TABLE IF NOT EXISTS trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    ticker TEXT NOT NULL,
    direction TEXT,
    entry_low NUMERIC,
    entry_high NUMERIC,
    stop_loss NUMERIC,
    targets NUMERIC[],
    position_size_pct NUMERIC,
    ev_pct NUMERIC,
    risk_verdict TEXT,
    timing_signal TEXT,
    veto_path TEXT,
    status TEXT DEFAULT 'pending',          -- 'pending' | 'pending_review' | 'approved' | 'blocked' | 'executed'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    executed_at TIMESTAMPTZ
);

-- Portfolio state
CREATE TABLE IF NOT EXISTS portfolio (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    state JSONB NOT NULL,
    last_verified_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    updated_by TEXT DEFAULT 'operator'
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_analyses_ticker ON analyses(ticker);
CREATE INDEX IF NOT EXISTS idx_analyses_stale ON analyses(stale_after);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_threads_workspace ON threads(workspace_id);
CREATE INDEX IF NOT EXISTS idx_verdict_cache_ticker ON verdict_cache(ticker);
CREATE INDEX IF NOT EXISTS idx_verdict_cache_verdict ON verdict_cache(verdict);
CREATE INDEX IF NOT EXISTS idx_verdict_cache_checklist ON verdict_cache USING gin(checklist);
CREATE INDEX IF NOT EXISTS idx_verdict_cache_signals ON verdict_cache USING gin(signals);
CREATE INDEX IF NOT EXISTS idx_verdict_cache_stale ON verdict_cache(stale_after);
CREATE INDEX IF NOT EXISTS idx_checkpoints_thread ON checkpoints(thread_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_status ON checkpoints(status);

-- Sample verdict cache queries (reference only)
-- All REVIEW names where only failing item was concentration:
-- SELECT ticker, analysis_date, score, checklist FROM verdict_cache
-- WHERE verdict = 'REVIEW' AND checklist->>'concentration' = 'FAIL'
--   AND (SELECT COUNT(*) FROM jsonb_each_text(checklist) WHERE value = 'FAIL') = 1
--   AND stale_after > NOW() ORDER BY analysis_date DESC;

-- Names pending operator review:
-- SELECT ticker, created_at, veto_path FROM trades WHERE status = 'pending_review' ORDER BY created_at ASC;
