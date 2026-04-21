-- R4: time-series priors for the Opus curator.
--
-- Phase 5 already injects live calibration into the prompt (buckets,
-- strategy types, gate bias). Those views are always "now" — no way to
-- tell whether the curator is improving. This migration adds a snapshot
-- table that a nightly job appends to, plus a view that surfaces the last
-- N weeks so the curator can see its own trend.

CREATE TABLE IF NOT EXISTS curator_priors_snapshot (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_date       DATE NOT NULL DEFAULT CURRENT_DATE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  dimension           TEXT NOT NULL,        -- 'bucket' | 'strategy_type' | 'gate'
  key                 TEXT NOT NULL,        -- 'high', 'momentum', 'paperhunter', …
  n_rated             INT,
  n_with_truth        INT,
  n_promoted          INT,
  n_backtest_pass     INT,
  n_hunter_rejected   INT,
  promotion_rate      NUMERIC(4,3),
  avg_confidence      NUMERIC(4,3),
  avg_predicted       NUMERIC(4,3),         -- for gate dimension
  actual_pass_rate    NUMERIC(4,3),         -- for gate dimension
  over_confidence_bias NUMERIC(4,3),        -- for gate dimension
  UNIQUE (snapshot_date, dimension, key)
);

CREATE INDEX IF NOT EXISTS curator_priors_snapshot_dim_key_idx
  ON curator_priors_snapshot (dimension, key, snapshot_date DESC);

-- Last 8 weekly snapshots (for the curator prompt: "your high-bucket
-- promotion rate: wk-1 0.48, wk-2 0.52, wk-3 …").
CREATE OR REPLACE VIEW curator_priors_trend AS
SELECT
  dimension,
  key,
  snapshot_date,
  promotion_rate,
  actual_pass_rate,
  over_confidence_bias,
  n_rated,
  n_with_truth
FROM curator_priors_snapshot
WHERE snapshot_date >= CURRENT_DATE - INTERVAL '56 days'
ORDER BY dimension, key, snapshot_date DESC;
