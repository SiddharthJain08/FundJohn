-- 159_proposal_calibration.sql
-- Spec docs/specs/2026-09-12-quantdinger-adoptions-spec.md §4 D2 (item 7).
-- Records what auto_approve actually compared: the mastermind's stated
-- confidence, the outcome-calibrated version of it, the evidence cap, and
-- which of the two bounds bit. Written in BOTH the shadow and the enforced
-- path so the operator can read two weekends of evidence before flipping
-- OPENCLAW_PROPOSAL_CALIBRATED=1.
--
-- Additive only: no column is dropped, no row is rewritten (repo CLAUDE.md
-- append-only invariant). Streams B and C hold 155-158.

ALTER TABLE strategy_regime_param_proposals
    ADD COLUMN IF NOT EXISTS confidence_raw        NUMERIC,
    ADD COLUMN IF NOT EXISTS confidence_calibrated NUMERIC,
    ADD COLUMN IF NOT EXISTS evidence_cap          NUMERIC,
    ADD COLUMN IF NOT EXISTS evidence_level        TEXT,
    ADD COLUMN IF NOT EXISTS binding_bound         TEXT;

COMMENT ON COLUMN strategy_regime_param_proposals.confidence_raw IS
    'confidence as stated by the mastermind, snapshotted at auto-approval time';
COMMENT ON COLUMN strategy_regime_param_proposals.confidence_calibrated IS
    'raw x clip(bucket match_rate / bucket midpoint, 0.5, 1.0) when the bucket has n >= 8, else raw';
COMMENT ON COLUMN strategy_regime_param_proposals.evidence_cap IS
    'cap from the decisive-window closed-trade count + staleness: none .35 / low .55 / medium .75 / high 1.0';
COMMENT ON COLUMN strategy_regime_param_proposals.evidence_level IS
    'none | low | medium | high';
COMMENT ON COLUMN strategy_regime_param_proposals.binding_bound IS
    'calibrated | cap — which of the two produced min(calibrated, cap)';
