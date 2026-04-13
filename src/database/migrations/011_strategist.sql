-- Research sessions: each off-hours run is a session
CREATE TABLE research_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id),
    status TEXT DEFAULT 'active',
    phase TEXT DEFAULT 'EXPLORE',
    started_at TIMESTAMPTZ DEFAULT NOW(),
    paused_at TIMESTAMPTZ,
    resumed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    total_tokens_used INTEGER DEFAULT 0,
    pause_reason TEXT,
    session_notes TEXT,
    state JSONB DEFAULT '{}'
);

-- Strategy hypotheses generated during exploration
CREATE TABLE strategy_hypotheses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES research_sessions(id),
    workspace_id UUID REFERENCES workspaces(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    tier TEXT,
    data_requirements JSONB,
    implementation_complexity TEXT,
    hypothesis_score NUMERIC,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Backtest results
CREATE TABLE backtest_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hypothesis_id UUID REFERENCES strategy_hypotheses(id),
    workspace_id UUID REFERENCES workspaces(id),
    backtest_period_start DATE NOT NULL,
    backtest_period_end DATE NOT NULL,
    universe TEXT NOT NULL,
    total_trades INTEGER,
    win_rate NUMERIC,
    avg_win_pct NUMERIC,
    avg_loss_pct NUMERIC,
    sharpe_ratio NUMERIC,
    max_drawdown_pct NUMERIC,
    annualized_return_pct NUMERIC,
    benchmark_return_pct NUMERIC,
    information_ratio NUMERIC,
    calmar_ratio NUMERIC,
    avg_holding_days NUMERIC,
    profit_factor NUMERIC,
    walk_forward_score NUMERIC,
    statistical_significance NUMERIC,
    passed_validation BOOLEAN DEFAULT FALSE,
    rejection_reason TEXT,
    full_results JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Published strategy reports
CREATE TABLE strategy_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hypothesis_id UUID REFERENCES strategy_hypotheses(id),
    workspace_id UUID REFERENCES workspaces(id),
    title TEXT NOT NULL,
    report_path TEXT NOT NULL,
    discord_message_id TEXT,
    discord_channel_id TEXT,
    published_at TIMESTAMPTZ,
    status TEXT DEFAULT 'pending',
    operator_feedback TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Emergency alerts for position risk
CREATE TABLE emergency_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id),
    ticker TEXT,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence JSONB NOT NULL,
    report_path TEXT,
    discord_message_id TEXT,
    acknowledged BOOLEAN DEFAULT FALSE,
    acknowledged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Strategist self-learning
CREATE TABLE research_utility (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id),
    direction TEXT NOT NULL,
    data_sources TEXT[],
    hypotheses_generated INTEGER DEFAULT 0,
    hypotheses_validated INTEGER DEFAULT 0,
    hypotheses_published INTEGER DEFAULT 0,
    avg_sharpe_of_validated NUMERIC,
    utility_score NUMERIC DEFAULT 50,
    last_explored TIMESTAMPTZ,
    notes TEXT,
    UNIQUE(workspace_id, direction)
);

-- Token usage tracking per calendar day
CREATE TABLE token_usage_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id),
    usage_date DATE NOT NULL DEFAULT CURRENT_DATE,
    agent_type TEXT NOT NULL,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    total_tokens INTEGER GENERATED ALWAYS AS (tokens_in + tokens_out) STORED,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_token_usage_date ON token_usage_log(usage_date, workspace_id);

-- Indexes
CREATE INDEX idx_research_sessions_status ON research_sessions(status);
CREATE INDEX idx_strategy_hypotheses_status ON strategy_hypotheses(status);
CREATE INDEX idx_backtest_results_hypothesis ON backtest_results(hypothesis_id);
CREATE INDEX idx_backtest_results_passed ON backtest_results(passed_validation);
CREATE INDEX idx_strategy_reports_status ON strategy_reports(status);
CREATE INDEX idx_emergency_alerts_acknowledged ON emergency_alerts(acknowledged);
CREATE INDEX idx_research_utility_score ON research_utility(utility_score DESC);
