-- Phase 1 instrumentation: structured per-gate decisions across the full research
-- funnel. Populated by research-orchestrator.js hooks and the backfill script.

CREATE TABLE IF NOT EXISTS paper_gate_decisions (
  decision_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  paper_id       UUID REFERENCES research_corpus(paper_id),   -- nullable: pre-corpus candidates
  candidate_id   UUID REFERENCES research_candidates(candidate_id),
  strategy_id    TEXT,                                        -- set once StrategyCoder picks a name
  gate_name      TEXT NOT NULL,
    -- Enumerated values (informational, not enforced):
    --   'curator'           Opus corpus curator bucket decision
    --   'paperhunter_g1'    non_deterministic gate
    --   'paperhunter_g2'    overfitting_risk gate
    --   'paperhunter_g3'    duplicate_fingerprint gate
    --   'paperhunter_g4'    capability_gap gate
    --   'paperhunter'       aggregate pass/reject from PaperHunter
    --   'researchjohn'      READY / BUILDABLE / BLOCKED classification
    --   'validate'          validate_strategy.py contract gate
    --   'convergence'       auto_backtest.py 3-window convergence
    --   'promotion'         lifecycle transition candidate -> paper
  outcome        TEXT NOT NULL,
    -- 'pass' | 'reject' | 'buildable' | 'error'
  reason_code    TEXT,
    -- Structured enum for analytics: 'non_deterministic', 'overfitting_risk',
    -- 'duplicate_fingerprint', 'capability_gap', 'sharpe_below_floor',
    -- 'dd_above_ceiling', 'trade_count_below_floor', 'windows_insufficient',
    -- 'validation_signature', 'validation_empty_df', 'fetch_failed', 'parse_failed',
    -- 'low_confidence' (curator), 'med_confidence' (curator), etc.
  reason_detail  TEXT,
  metadata       JSONB,                                       -- gate-specific payload
  occurred_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS paper_gate_decisions_paper_idx     ON paper_gate_decisions (paper_id);
CREATE INDEX IF NOT EXISTS paper_gate_decisions_candidate_idx ON paper_gate_decisions (candidate_id);
CREATE INDEX IF NOT EXISTS paper_gate_decisions_gate_idx      ON paper_gate_decisions (gate_name, outcome);
CREATE INDEX IF NOT EXISTS paper_gate_decisions_occurred_idx  ON paper_gate_decisions (occurred_at DESC);

-- Funnel view: one row per paper_id with counts/pass flags at each stage.
-- Phase 1 !hit-rate command reads this.
CREATE OR REPLACE VIEW paper_hit_rate_funnel AS
SELECT
  p.paper_id,
  p.source,
  p.ingested_at,
  BOOL_OR(d.gate_name = 'curator'      AND d.outcome = 'pass') AS curator_high,
  BOOL_OR(d.gate_name = 'paperhunter'  AND d.outcome = 'pass') AS hunter_pass,
  BOOL_OR(d.gate_name = 'researchjohn' AND d.outcome = 'pass') AS classified_ready,
  BOOL_OR(d.gate_name = 'validate'     AND d.outcome = 'pass') AS validated,
  BOOL_OR(d.gate_name = 'convergence'  AND d.outcome = 'pass') AS backtest_passed,
  BOOL_OR(d.gate_name = 'promotion'    AND d.outcome = 'pass') AS promoted
FROM research_corpus p
LEFT JOIN paper_gate_decisions d USING (paper_id)
GROUP BY p.paper_id, p.source, p.ingested_at;
