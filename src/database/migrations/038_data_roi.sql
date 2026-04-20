-- Phase 5d: missing-data ROI dashboard.
--
-- For each data category that blocks papers, estimate the value of buying a
-- provider:
--   unlock_value = blocked_papers * conditional_promotion_rate
-- Divide by provider cost to rank by $ per expected promotion.
--
-- The conditional_promotion_rate is the observed promotion rate for papers
-- with the SAME strategy types as the blocked ones, drawn from curator
-- calibration. If that signal is thin, we fall back to the global high-bucket
-- promotion rate. Clamped to a floor so we don't divide by zero.

CREATE OR REPLACE VIEW data_category_unlock_estimate AS
WITH -- Per-category blocked-paper list + predominant strategy types
cat_papers AS (
  SELECT data_category, paper_id
    FROM missing_data_features
   WHERE data_category IS NOT NULL
   GROUP BY data_category, paper_id
),
cat_types AS (
  SELECT cp.data_category,
         pst.strategy_type,
         COUNT(DISTINCT cp.paper_id) AS n_papers
    FROM cat_papers cp
    LEFT JOIN paper_strategy_types pst USING (paper_id)
   GROUP BY cp.data_category, pst.strategy_type
),
-- Per-category baseline: expected promotion rate of papers in this category,
-- weighted by their strategy-type distribution.
global_prom_rate AS (
  SELECT ROUND(
           COALESCE(
             SUM(n_promoted)::numeric / NULLIF(SUM(n_with_truth), 0),
             0
           ), 4
         ) AS p
    FROM strategy_type_calibration
),
cat_rate AS (
  SELECT
    ct.data_category,
    ROUND(
      COALESCE(
        SUM(ct.n_papers * COALESCE(stc.promotion_rate, (SELECT p FROM global_prom_rate)))
        / NULLIF(SUM(ct.n_papers), 0),
        (SELECT p FROM global_prom_rate)
      ), 4
    ) AS expected_promotion_rate,
    SUM(ct.n_papers) AS blocked_papers
  FROM cat_types ct
  LEFT JOIN strategy_type_calibration stc USING (strategy_type)
  GROUP BY ct.data_category
)
SELECT
  cr.data_category,
  cr.blocked_papers,
  cr.expected_promotion_rate,
  -- Conservative: assume the published rate even if unlocked. A strict upper bound.
  ROUND(cr.blocked_papers * cr.expected_promotion_rate, 2) AS expected_unlocks,
  dpr.suggested_providers,
  dpr.est_monthly_cost_usd,
  -- ROI = blocked_papers * expected_rate / monthly_cost. Higher = better.
  CASE
    WHEN dpr.est_monthly_cost_usd IS NULL OR dpr.est_monthly_cost_usd <= 0 THEN NULL
    ELSE ROUND(
      (cr.blocked_papers * cr.expected_promotion_rate) / dpr.est_monthly_cost_usd * 1000,
      3
    )
  END AS expected_unlocks_per_1k_usd,
  dpr.notes
FROM cat_rate cr
LEFT JOIN data_provider_recommendations dpr USING (data_category)
ORDER BY expected_unlocks_per_1k_usd DESC NULLS LAST, cr.blocked_papers DESC;
