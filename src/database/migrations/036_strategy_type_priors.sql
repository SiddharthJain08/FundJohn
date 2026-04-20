-- Phase 5b: strategy-type priors.
--
-- Classifies papers into coarse strategy types from title + abstract keywords.
-- Each paper is tagged with the FIRST matching type (so multi-topic papers
-- get the most distinctive label). The per-type promotion rate, computed
-- from paper_gate_decisions, is injected into the curator prompt so the
-- model sees its own empirical accuracy on each strategy category.
--
-- Classification is deliberately crude (SQL LIKE heuristics). Refinement via
-- LLM tagging is a later option; the goal here is a calibration prior, not a
-- precise research taxonomy.

CREATE OR REPLACE VIEW paper_strategy_types AS
SELECT
  p.paper_id,
  CASE
    -- Order matters: first match wins, most specific categories first.
    WHEN p.title ILIKE '%high-frequency%' OR p.abstract ILIKE '%high-frequency%'
      OR p.abstract ILIKE '%tick data%' OR p.abstract ILIKE '%order book%'
      OR p.abstract ILIKE '%limit order%' OR p.title ILIKE '%HFT%'
      THEN 'microstructure'

    WHEN p.title ILIKE '%options%' OR p.abstract ILIKE '%implied volatility%'
      OR p.abstract ILIKE '%variance swap%' OR p.abstract ILIKE '%option pricing%'
      OR p.abstract ILIKE '%straddle%' OR p.abstract ILIKE '%volatility surface%'
      THEN 'options'

    WHEN p.title ILIKE '%volatility%' OR p.abstract ILIKE '%realized volatility%'
      OR p.abstract ILIKE '%variance risk premium%' OR p.abstract ILIKE '%GARCH%'
      THEN 'volatility'

    WHEN p.abstract ILIKE '%machine learning%' OR p.abstract ILIKE '%deep learning%'
      OR p.abstract ILIKE '%neural network%' OR p.abstract ILIKE '%LSTM%'
      OR p.abstract ILIKE '%transformer%' OR p.abstract ILIKE '%gradient boost%'
      OR p.abstract ILIKE '%random forest%' OR p.abstract ILIKE '%LLM%'
      OR p.title ILIKE '%machine learning%'
      THEN 'ml_based'

    WHEN p.abstract ILIKE '%momentum%' OR p.title ILIKE '%momentum%'
      OR p.abstract ILIKE '%trend following%' OR p.abstract ILIKE '%time-series momentum%'
      THEN 'momentum'

    WHEN p.abstract ILIKE '%value factor%' OR p.abstract ILIKE '%book-to-market%'
      OR p.abstract ILIKE '%value premium%' OR p.title ILIKE '%value investing%'
      THEN 'value'

    WHEN p.abstract ILIKE '%earnings announcement%' OR p.abstract ILIKE '%post-earnings%'
      OR p.abstract ILIKE '%event study%' OR p.abstract ILIKE '%merger%'
      OR p.abstract ILIKE '%acquisition%' OR p.abstract ILIKE '%IPO%'
      OR p.abstract ILIKE '%spinoff%'
      THEN 'event_driven'

    WHEN p.abstract ILIKE '%factor model%' OR p.abstract ILIKE '%fama-french%'
      OR p.abstract ILIKE '%multifactor%' OR p.abstract ILIKE '%cross-section%'
      OR p.abstract ILIKE '%risk premia%' OR p.abstract ILIKE '%characteristic%'
      THEN 'factor_classical'

    WHEN p.abstract ILIKE '%insider%' OR p.abstract ILIKE '%13F%'
      OR p.abstract ILIKE '%institutional%' OR p.abstract ILIKE '%short interest%'
      OR p.abstract ILIKE '%hedge fund%'
      THEN 'positioning_ownership'

    WHEN p.abstract ILIKE '%macroeconomic%' OR p.abstract ILIKE '%monetary policy%'
      OR p.abstract ILIKE '%yield curve%' OR p.abstract ILIKE '%inflation%'
      OR p.abstract ILIKE '%GDP%' OR p.abstract ILIKE '%central bank%'
      THEN 'macro'

    WHEN p.abstract ILIKE '%sentiment%' OR p.abstract ILIKE '%news%'
      OR p.abstract ILIKE '%text%' OR p.abstract ILIKE '%NLP%'
      OR p.abstract ILIKE '%natural language%'
      THEN 'text_sentiment'

    WHEN p.abstract ILIKE '%portfolio%' OR p.abstract ILIKE '%asset allocation%'
      OR p.abstract ILIKE '%mean-variance%' OR p.abstract ILIKE '%Sharpe ratio%'
      OR p.abstract ILIKE '%minimum variance%'
      THEN 'portfolio_construction'

    ELSE 'other'
  END AS strategy_type
FROM research_corpus p;

-- Per-type calibration: how often does each strategy type promote?
-- Only includes papers with a rating + downstream truth.
CREATE OR REPLACE VIEW strategy_type_calibration AS
SELECT
  pst.strategy_type,
  COUNT(DISTINCT cc.paper_id)                            AS n_rated,
  COUNT(DISTINCT cc.paper_id) FILTER (WHERE t.paper_id IS NOT NULL) AS n_with_truth,
  COUNT(DISTINCT cc.paper_id) FILTER (WHERE t.promoted)  AS n_promoted,
  COUNT(DISTINCT cc.paper_id) FILTER (WHERE t.backtest_passed) AS n_backtest_pass,
  COUNT(DISTINCT cc.paper_id) FILTER (WHERE t.hunter_rejected) AS n_hunter_rejected,
  ROUND(
    CASE WHEN COUNT(*) FILTER (WHERE t.paper_id IS NOT NULL) = 0 THEN NULL
         ELSE COUNT(*) FILTER (WHERE t.promoted)::numeric
              / NULLIF(COUNT(*) FILTER (WHERE t.paper_id IS NOT NULL), 0)
    END, 3
  )::numeric(4,3) AS promotion_rate,
  ROUND(AVG(cc.confidence), 3)::numeric(4,3)             AS avg_confidence,
  ROUND(
    AVG(CASE WHEN cc.predicted_bucket='high' THEN 1.0 ELSE 0.0 END), 3
  )::numeric(4,3) AS high_bucket_fraction
FROM paper_strategy_types pst
JOIN curated_candidates cc USING (paper_id)
LEFT JOIN paper_truth_flags t USING (paper_id)
GROUP BY pst.strategy_type
ORDER BY n_rated DESC;
