-- Phase 2A — Renaissance IC Approval Gate.
--
-- Append-only table for every operator-approval decision the IC gate
-- emits between the `signals` and `handoff` pipeline steps. AUTO_APPROVE
-- rows are inserted for audit; IC_REQUIRED rows start with decided_by
-- NULL and flip to APPROVED / VETOED / SCALED / TIMED_OUT when the
-- operator types `approve N` / `veto N` / `scale N PCT` in
-- #ic-approvals (handler: src/channels/discord/ic_handler.js).
--
-- VETOED on creation = strategy is deprecated/archived OR the signal
-- was malformed; these are decided synchronously by the classifier
-- (src/execution/ic_gate.py) with no operator interaction.
--
-- Default-OFF: only populated when OPENCLAW_IC_GATE=1.
-- Spec: docs/superpowers/plans/2026-05-15-fincept-imports-phase-2-master-plan.md
--       (Project 2A, tasks A2.2 / A2.5 / A2.6)

CREATE TABLE IF NOT EXISTS ic_decisions (
    id              BIGSERIAL PRIMARY KEY,
    run_date        DATE NOT NULL,
    strategy_id     TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    classification  TEXT NOT NULL CHECK (classification IN (
                        'AUTO_APPROVE','IC_REQUIRED','APPROVED',
                        'VETOED','SCALED','TIMED_OUT')),
    reason          TEXT,
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    scaled_size_pct DOUBLE PRECISION,
    raw_signal      JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ic_decisions_run
    ON ic_decisions (run_date, strategy_id);

-- Partial index for the runner's poll loop: only scan rows still awaiting
-- an operator decision. The handler UPDATE moves rows out of this index
-- (decided_at IS NOT NULL).
CREATE INDEX IF NOT EXISTS idx_ic_decisions_pending
    ON ic_decisions (run_date)
    WHERE classification = 'IC_REQUIRED' AND decided_at IS NULL;
