-- Data collection tasks queued by the data agent
CREATE TABLE IF NOT EXISTS data_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id),
    description TEXT NOT NULL,           -- operator's natural language request
    status TEXT DEFAULT 'queued',        -- queued | running | complete | partial | failed
    queued_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,

    -- What the agent decided to collect
    plan JSONB,
    -- {
    --   "datasets": [
    --     {"name": "prices", "tickers": ["AAPL","MSFT"], "lookback_days": 365, "provider": "polygon"},
    --     {"name": "insider", "tickers": ["AAPL"], "lookback_days": 180, "provider": "sec_edgar"}
    --   ]
    -- }

    -- Results after collection
    collected JSONB,                     -- what was successfully collected
    unavailable JSONB,                   -- what failed and why
    rows_added INTEGER DEFAULT 0,
    discord_message_id TEXT,             -- message ID in #data-alerts for live edits
    requested_by TEXT,                   -- Discord user who requested it
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_data_tasks_status    ON data_tasks(status, queued_at DESC);
CREATE INDEX IF NOT EXISTS idx_data_tasks_workspace ON data_tasks(workspace_id);
