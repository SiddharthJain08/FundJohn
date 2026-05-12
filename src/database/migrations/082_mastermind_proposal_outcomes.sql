-- 082_mastermind_proposal_outcomes.sql
-- Tracks live performance pre/post each Mastermind proposal decision.
-- Phase 2D: feeds the confidence-calibration report.

CREATE TABLE IF NOT EXISTS mastermind_proposal_outcomes (
    proposal_id         BIGINT       PRIMARY KEY REFERENCES strategy_regime_param_proposals(id),
    outcome_window_days INTEGER      NOT NULL,
    decided_at          TIMESTAMPTZ  NOT NULL,
    decision_status     TEXT         NOT NULL,
    confidence          NUMERIC,
    live_sharpe_pre     NUMERIC,
    live_sharpe_post    NUMERIC,
    live_pnl_delta      NUMERIC,
    direction_match     BOOLEAN,
    computed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mpo_decided_at
    ON mastermind_proposal_outcomes (decided_at DESC);
