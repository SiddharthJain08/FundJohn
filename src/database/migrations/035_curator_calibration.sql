-- Phase 3: calibration loop for the Opus corpus curator.
-- Exposes views that compute how well each predicted bucket actually performed
-- against the downstream gate history. corpus_curator.js reads these to inject
-- a "your recent calibration" section into the prompt on each run.

-- Helper: collapse paper_gate_decisions into one row per paper with boolean
-- flags for each gate outcome. Used by the calibration view AND the dry-run
-- calibration report in corpus_curator.js.
CREATE OR REPLACE VIEW paper_truth_flags AS
SELECT
  paper_id,
  BOOL_OR(gate_name = 'paperhunter'  AND outcome = 'pass')  AS hunter_passed,
  BOOL_OR(gate_name = 'paperhunter'  AND outcome = 'reject') AS hunter_rejected,
  BOOL_OR(gate_name = 'researchjohn' AND outcome = 'pass')  AS classified_ready,
  BOOL_OR(gate_name = 'validate'     AND outcome = 'pass')  AS validated,
  BOOL_OR(gate_name = 'convergence'  AND outcome = 'pass')  AS backtest_passed,
  BOOL_OR(gate_name = 'convergence'  AND outcome = 'reject') AS backtest_failed,
  BOOL_OR(gate_name = 'promotion'    AND outcome = 'pass')  AS promoted
FROM paper_gate_decisions
WHERE paper_id IS NOT NULL
GROUP BY paper_id;

-- Per-bucket outcome stats. One row per (bucket, lookback). Powers the live
-- calibration block injected into the curator prompt.
CREATE OR REPLACE VIEW curator_bucket_calibration AS
SELECT
  cc.predicted_bucket,
  COUNT(*)::int                                                  AS n_rated,
  COUNT(*) FILTER (WHERE t.paper_id IS NOT NULL)::int            AS n_with_truth,
  COUNT(*) FILTER (WHERE t.promoted)::int                        AS n_promoted,
  COUNT(*) FILTER (WHERE t.backtest_passed)::int                 AS n_backtest_pass,
  COUNT(*) FILTER (WHERE t.hunter_rejected)::int                 AS n_hunter_rejected,
  ROUND(
    CASE WHEN COUNT(*) FILTER (WHERE t.paper_id IS NOT NULL) = 0 THEN NULL
         ELSE COUNT(*) FILTER (WHERE t.promoted)::numeric
              / NULLIF(COUNT(*) FILTER (WHERE t.paper_id IS NOT NULL), 0)
    END, 3
  )::numeric(4,3) AS promotion_rate,
  MAX(cc.created_at)                                             AS latest_eval
FROM curated_candidates cc
LEFT JOIN paper_truth_flags t ON t.paper_id = cc.paper_id
GROUP BY cc.predicted_bucket
ORDER BY CASE cc.predicted_bucket
           WHEN 'high' THEN 1 WHEN 'med' THEN 2
           WHEN 'low' THEN 3 WHEN 'reject' THEN 4 ELSE 9
         END;

-- False positives: curator marked HIGH but downstream rejected.
CREATE OR REPLACE VIEW curator_false_positives AS
SELECT
  cc.paper_id,
  cc.run_id,
  cc.confidence,
  cc.reasoning,
  cc.predicted_failure_modes,
  p.title,
  p.source,
  t.hunter_rejected,
  t.backtest_failed,
  t.promoted,
  cc.created_at
FROM curated_candidates cc
JOIN research_corpus p USING (paper_id)
JOIN paper_truth_flags t USING (paper_id)
WHERE cc.predicted_bucket = 'high'
  AND NOT t.promoted
  AND (t.hunter_rejected OR t.backtest_failed)
ORDER BY cc.created_at DESC;

-- False negatives: curator marked LOW/REJECT but paper actually promoted.
-- Rare (because low/reject papers don't enter the pipeline), so any entry
-- here is valuable signal for re-calibration.
CREATE OR REPLACE VIEW curator_false_negatives AS
SELECT
  cc.paper_id,
  cc.run_id,
  cc.confidence,
  cc.predicted_bucket,
  cc.reasoning,
  cc.predicted_failure_modes,
  p.title,
  p.source,
  cc.created_at
FROM curated_candidates cc
JOIN research_corpus p USING (paper_id)
JOIN paper_truth_flags t USING (paper_id)
WHERE cc.predicted_bucket IN ('low', 'reject')
  AND t.promoted
ORDER BY cc.created_at DESC;
