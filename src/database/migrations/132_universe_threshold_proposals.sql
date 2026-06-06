-- 132: universe_threshold_proposals (SP-7 Phase B B3, 2026-06-06)
--
-- √ln(N) breadth-scaled min_cumulative_sharpe PROPOSALS (never direct writes).
-- proposed = current_base × √(ln N_union / ln N_sp500), clamped [1.0, 10.0].
-- Mimics strategy_regime_param_proposals' shape (mig 078) but targets the
-- GLOBAL regime_sizer_params table, so no strategy_id column.

CREATE TABLE IF NOT EXISTS universe_threshold_proposals (
    id                              BIGSERIAL    PRIMARY KEY,
    proposed_at                     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    proposer                        TEXT         NOT NULL,   -- 'sp7b:<trigger>'
    regime_state                    TEXT         NOT NULL,   -- LOW_VOL|TRANSITIONING|HIGH_VOL|CRISIS
    current_row                     JSONB,                   -- regime_sizer_params snapshot
    proposed_min_cumulative_sharpe  NUMERIC      NOT NULL,
    basis                           JSONB,                   -- {n_union, n_sp500, factor, trigger}
    status                          TEXT         NOT NULL DEFAULT 'pending',
                                    -- pending | approved | rejected | superseded
    decided_at                      TIMESTAMPTZ,
    decided_by                      TEXT,
    decision_reason                 TEXT,
    applied_row                     JSONB
);

CREATE INDEX IF NOT EXISTS idx_utp_status
    ON universe_threshold_proposals (status, proposed_at DESC);
