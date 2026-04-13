-- Token usage and cost tracking

-- Per-task totals (one row per user-initiated task: /diligence, /fetch, /trade, etc.)
CREATE TABLE IF NOT EXISTS task_costs (
    id          BIGSERIAL PRIMARY KEY,
    task_id     UUID NOT NULL UNIQUE,          -- threadId from bot.js
    task_type   TEXT NOT NULL,                 -- 'diligence', 'data-fetch', 'trade', 'general'
    ticker      TEXT,
    status      TEXT NOT NULL DEFAULT 'running', -- 'running', 'complete', 'failed'
    cost_usd    NUMERIC(10, 6) DEFAULT 0,      -- running total, updated as subagents complete
    est_cost_usd NUMERIC(10, 6),               -- pre-task estimate
    duration_ms  INTEGER,
    num_subagents INTEGER DEFAULT 0,
    started_at  TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- Per-subagent detail (one row per claude-bin invocation)
CREATE TABLE IF NOT EXISTS subagent_costs (
    id           BIGSERIAL PRIMARY KEY,
    task_id      UUID NOT NULL REFERENCES task_costs(task_id) ON DELETE CASCADE,
    subagent_id  UUID NOT NULL,
    subagent_type TEXT NOT NULL,               -- 'research', 'data-prep', 'equity-analyst', etc.
    ticker       TEXT,
    model        TEXT,
    cost_usd     NUMERIC(10, 6),
    duration_ms  INTEGER,
    num_turns    INTEGER,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_costs_type     ON task_costs(task_type, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_costs_ticker   ON task_costs(ticker, task_type);
CREATE INDEX IF NOT EXISTS idx_subagent_costs_task ON subagent_costs(task_id);
CREATE INDEX IF NOT EXISTS idx_subagent_costs_type ON subagent_costs(subagent_type, created_at DESC);
