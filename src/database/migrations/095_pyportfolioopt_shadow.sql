-- Phase 1G — PyPortfolioOpt shadow-sizer run capture.
-- Append-only.  Compared offline to regime_blended_sizer_live decisions.
-- Default OFF: gated by OPENCLAW_PYPORTFOLIOOPT_SHADOW=1.

CREATE TABLE IF NOT EXISTS pyportfolioopt_shadow_runs (
    id                  BIGSERIAL PRIMARY KEY,
    run_date            DATE NOT NULL,
    method              TEXT NOT NULL,        -- 'hrp' | 'black_litterman' | 'efficient_cvar'
    handoff_signals_n   INT NOT NULL,
    equity_usd          DOUBLE PRECISION NOT NULL,
    weights             JSONB NOT NULL,
    target_dollars      JSONB NOT NULL,
    live_dollars        JSONB NOT NULL,
    diff_dollars        JSONB NOT NULL,
    diversification_ratio DOUBLE PRECISION,
    expected_vol_pct    DOUBLE PRECISION,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, method)
);

CREATE INDEX IF NOT EXISTS idx_ppo_shadow_run_date ON pyportfolioopt_shadow_runs (run_date DESC);
