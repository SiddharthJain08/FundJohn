-- 078_strategy_regime_param_proposals.sql
-- Pending Mastermind proposals for per-(strategy, regime) parameter changes.
-- Phase 2B: proposer is MastermindJohn comprehensive-review (Saturday 18:00 ET);
-- approver is the operator via dashboard/CLI. Approved proposals write through
-- to strategy_regime_params via the eligibility_manager transaction.

CREATE TABLE IF NOT EXISTS strategy_regime_param_proposals (
    id                       BIGSERIAL    PRIMARY KEY,
    proposed_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    proposer                 TEXT         NOT NULL,    -- 'mastermind:<run_id>' | 'operator:<name>'
    strategy_id              TEXT         NOT NULL,
    regime_state             TEXT         NOT NULL,    -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    current_row              JSONB,                    -- snapshot of strategy_regime_params at proposal time
    proposed_eligible        BOOLEAN,
    proposed_size_scalar     NUMERIC,
    proposed_stop_pct        NUMERIC,
    proposed_target_pct      NUMERIC,
    proposed_max_hold_days   INTEGER,
    confidence               NUMERIC,                  -- 0.0 - 1.0
    reasoning                TEXT,                     -- short justification from Mastermind
    memo_id                  UUID,                     -- FK to strategy_memos.id (NULL for operator-created)
    status                   TEXT         NOT NULL DEFAULT 'pending',
                                                       -- 'pending' | 'approved' | 'rejected' | 'modified' | 'superseded'
    decided_at               TIMESTAMPTZ,
    decided_by               TEXT,
    decision_reason          TEXT,
    applied_row              JSONB                     -- snapshot of what landed in strategy_regime_params
);

CREATE INDEX IF NOT EXISTS idx_srpp_status
    ON strategy_regime_param_proposals (status, proposed_at DESC);

CREATE INDEX IF NOT EXISTS idx_srpp_strategy_regime
    ON strategy_regime_param_proposals (strategy_id, regime_state, proposed_at DESC);
