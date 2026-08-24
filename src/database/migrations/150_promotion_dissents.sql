-- 150: structured adversarial dissent at candidate->live promotion (task S3,
-- five-repo-adoptions, 2026-08-24). Advisory-only, append-only log: one row
-- per candidate->live transition the fully-automatic promotion pipeline
-- (src/agent/curators/auto_approval.js sweepCandidates) confirms successful.
-- NON-VETO BY DESIGN — this table is written AFTER the promotion is already
-- final; nothing here can block or reverse a transition. A structured LLM
-- dissent (regime-window undersampling, live-book correlation/crowding,
-- economic-mechanism plausibility vs curve-fit, cost/capacity at larger
-- size, tail profile) is recorded for human review, never as a gate input.
-- Idempotent (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS),
-- mirrors the header style of 147_per_regime_min_acting_strategies.sql.
CREATE TABLE IF NOT EXISTS promotion_dissents (
  id BIGSERIAL PRIMARY KEY,
  strategy_id TEXT NOT NULL,
  promoted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  actor TEXT,                    -- e.g. system:sunday-auto-approval
  model TEXT,
  dissent JSONB NOT NULL,        -- [{concern, severity, evidence}]
  infra_fail BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_promotion_dissents_sid
  ON promotion_dissents (strategy_id, promoted_at DESC);

COMMENT ON TABLE promotion_dissents IS
  'advisory adversarial dissent, 2026-08-24 five-repo S3; never gates promotion';
