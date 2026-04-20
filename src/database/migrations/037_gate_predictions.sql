-- Phase 5c: per-gate pass-probability predictions.
--
-- Extends the curator output from a single confidence to per-gate predictions
-- so we can measure and correct calibration gate-by-gate. The overall
-- confidence becomes the product of the per-gate probs.
--
-- Schema: gate_predictions JSONB = {
--   paperhunter:  { pass_prob, reason },
--   researchjohn: { pass_prob, reason },
--   convergence:  { pass_prob, reason }
-- }

ALTER TABLE curated_candidates
  ADD COLUMN IF NOT EXISTS gate_predictions JSONB;

-- Per-gate calibration: compares predicted pass_prob with actual outcome at
-- each gate. Only papers with downstream truth are counted. Powers the
-- Phase 5c prompt extension and (eventually) a /curator gate-calibration report.
CREATE OR REPLACE VIEW curator_gate_calibration AS
WITH exp AS (
  SELECT
    cc.paper_id,
    cc.run_id,
    'paperhunter' AS gate_name,
    COALESCE((cc.gate_predictions->'paperhunter'->>'pass_prob')::numeric, NULL) AS predicted_prob,
    t.hunter_passed AS actual_pass,
    t.paper_id IS NOT NULL                AS has_truth
  FROM curated_candidates cc
  LEFT JOIN paper_truth_flags t USING (paper_id)
  WHERE cc.gate_predictions IS NOT NULL

  UNION ALL

  SELECT
    cc.paper_id,
    cc.run_id,
    'researchjohn' AS gate_name,
    COALESCE((cc.gate_predictions->'researchjohn'->>'pass_prob')::numeric, NULL),
    t.classified_ready,
    t.paper_id IS NOT NULL
  FROM curated_candidates cc
  LEFT JOIN paper_truth_flags t USING (paper_id)
  WHERE cc.gate_predictions IS NOT NULL

  UNION ALL

  SELECT
    cc.paper_id,
    cc.run_id,
    'convergence' AS gate_name,
    COALESCE((cc.gate_predictions->'convergence'->>'pass_prob')::numeric, NULL),
    t.backtest_passed,
    t.paper_id IS NOT NULL
  FROM curated_candidates cc
  LEFT JOIN paper_truth_flags t USING (paper_id)
  WHERE cc.gate_predictions IS NOT NULL
)
SELECT
  gate_name,
  COUNT(*) FILTER (WHERE has_truth AND predicted_prob IS NOT NULL)::int      AS n_observed,
  ROUND(AVG(predicted_prob) FILTER (WHERE has_truth AND predicted_prob IS NOT NULL), 3)::numeric(4,3) AS avg_predicted,
  ROUND(AVG(CASE WHEN actual_pass THEN 1.0 ELSE 0.0 END)
        FILTER (WHERE has_truth AND predicted_prob IS NOT NULL), 3)::numeric(4,3) AS actual_pass_rate,
  ROUND(
    AVG(predicted_prob) FILTER (WHERE has_truth AND predicted_prob IS NOT NULL)
    - AVG(CASE WHEN actual_pass THEN 1.0 ELSE 0.0 END) FILTER (WHERE has_truth AND predicted_prob IS NOT NULL),
    3
  )::numeric(4,3) AS over_confidence_bias  -- positive → overestimating, negative → underestimating
FROM exp
GROUP BY gate_name
ORDER BY gate_name;
