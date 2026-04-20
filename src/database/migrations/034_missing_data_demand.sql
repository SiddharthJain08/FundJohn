-- Phase 2b: missing-data-demand analytics.
-- Mines predicted_failure_modes from curated_candidates (and reason_code from
-- paper_gate_decisions) to surface which unavailable data features are blocking
-- the most papers. Informs data-provider investment decisions.

-- 1. Flattened view: one row per (paper, failure_mode) tuple across the latest
-- completed curator run per paper. Only the most recent evaluation counts, so
-- re-rated papers don't double-count against old buckets.
CREATE OR REPLACE VIEW paper_failure_modes AS
WITH latest_eval AS (
  SELECT DISTINCT ON (paper_id)
         paper_id, run_id, predicted_bucket, predicted_failure_modes, created_at
    FROM curated_candidates
    ORDER BY paper_id, created_at DESC
)
SELECT
  le.paper_id,
  le.run_id,
  le.predicted_bucket,
  le.created_at,
  UNNEST(le.predicted_failure_modes) AS failure_mode
FROM latest_eval le
WHERE le.predicted_failure_modes IS NOT NULL
  AND array_length(le.predicted_failure_modes, 1) > 0;

-- 2. Normalization + categorization. Turns "data_unavailable:foo", "data_unavailable:bar"
-- into a (category, feature_name) pair for aggregation. Kept as a view so the
-- mapping can be tuned without a migration.
CREATE OR REPLACE VIEW missing_data_features AS
SELECT
  pf.paper_id,
  pf.failure_mode,
  CASE
    -- Explicit data-unavailable tags
    WHEN pf.failure_mode LIKE 'data_unavailable:%' THEN
      split_part(pf.failure_mode, ':', 2)
    -- Non-explicit but interpretable categories
    WHEN pf.failure_mode = 'unavailable_alt_data'          THEN 'alternative_data'
    WHEN pf.failure_mode = 'non_us_equity'                 THEN 'non_us_equity'
    WHEN pf.failure_mode IN ('intraday_hf', 'high_frequency') THEN 'intraday_ohlcv'
    WHEN pf.failure_mode = 'factor_signals'                THEN 'factor_library'
    ELSE NULL  -- non-data failures (pure_theory, duplicate_of_*, etc.)
  END AS feature_name,
  CASE
    WHEN pf.failure_mode IN ('unavailable_alt_data',
                             'data_unavailable:audio_features',
                             'data_unavailable:satellite',
                             'data_unavailable:credit_card',
                             'data_unavailable:temperature',
                             'data_unavailable:web_scrape',
                             'data_unavailable:llm_narrative_classification',
                             'data_unavailable:narrative_tagged_jumps')
      THEN 'alternative_data'

    WHEN pf.failure_mode IN ('non_us_equity',
                             'data_unavailable:international',
                             'data_unavailable:non_us')
      THEN 'international_markets'

    WHEN pf.failure_mode IN ('data_unavailable:intraday_hf',
                             'data_unavailable:minute_bars',
                             'data_unavailable:second_bars',
                             'data_unavailable:L2_order_book',
                             'data_unavailable:TAQ',
                             'intraday_hf', 'high_frequency')
      THEN 'intraday_market_data'

    WHEN pf.failure_mode IN ('data_unavailable:options_microstructure',
                             'data_unavailable:options_order_book',
                             'data_unavailable:iv_surface_history')
      THEN 'options_microstructure'

    WHEN pf.failure_mode IN ('data_unavailable:factor_signals',
                             'factor_signals',
                             'data_unavailable:characteristics',
                             'data_unavailable:anomaly_library')
      THEN 'factor_library'

    WHEN pf.failure_mode IN ('data_unavailable:sentiment',
                             'data_unavailable:news_sentiment',
                             'data_unavailable:earnings_call_text',
                             'data_unavailable:filings_text',
                             'data_unavailable:analyst_tone')
      THEN 'text_sentiment'

    WHEN pf.failure_mode IN ('data_unavailable:analyst_forecast',
                             'data_unavailable:estimates',
                             'data_unavailable:consensus',
                             'data_unavailable:earnings_estimates')
      THEN 'analyst_estimates'

    WHEN pf.failure_mode IN ('data_unavailable:short_interest',
                             'data_unavailable:securities_lending',
                             'data_unavailable:borrow_fee',
                             'data_unavailable:13f',
                             'data_unavailable:institutional_holdings')
      THEN 'ownership_positioning'

    WHEN pf.failure_mode IN ('data_unavailable:futures',
                             'data_unavailable:fx',
                             'data_unavailable:crypto',
                             'data_unavailable:commodities',
                             'data_unavailable:bonds')
      THEN 'non_equity_asset_classes'

    WHEN pf.failure_mode IN ('data_unavailable:esg',
                             'data_unavailable:climate',
                             'data_unavailable:carbon',
                             'data_unavailable:temperature')
      THEN 'esg_climate'

    -- Substring heuristics for features that didn't match exact tags.
    -- Cheaper to catch here than to maintain exhaustive enumerations.
    WHEN pf.failure_mode LIKE '%transcript%'        THEN 'text_sentiment'
    WHEN pf.failure_mode LIKE '%sentiment%'         THEN 'text_sentiment'
    WHEN pf.failure_mode LIKE '%narrative%'         THEN 'text_sentiment'
    WHEN pf.failure_mode LIKE '%filings_text%'      THEN 'text_sentiment'
    WHEN pf.failure_mode LIKE '%news%'              THEN 'text_sentiment'
    WHEN pf.failure_mode LIKE '%earnings_call%'     THEN 'text_sentiment'

    WHEN pf.failure_mode LIKE '%intraday%'          THEN 'intraday_market_data'
    WHEN pf.failure_mode LIKE '%minute_bars%'       THEN 'intraday_market_data'
    WHEN pf.failure_mode LIKE '%second_bars%'       THEN 'intraday_market_data'
    WHEN pf.failure_mode LIKE '%order_book%'        THEN 'intraday_market_data'
    WHEN pf.failure_mode LIKE '%jump%'              THEN 'intraday_market_data'

    WHEN pf.failure_mode LIKE '%options_%'          THEN 'options_microstructure'
    WHEN pf.failure_mode LIKE '%iv_surface%'        THEN 'options_microstructure'

    WHEN pf.failure_mode LIKE '%esg%'               THEN 'esg_climate'
    WHEN pf.failure_mode LIKE '%climate%'           THEN 'esg_climate'
    WHEN pf.failure_mode LIKE '%carbon%'            THEN 'esg_climate'
    WHEN pf.failure_mode LIKE '%temperature%'       THEN 'esg_climate'
    WHEN pf.failure_mode LIKE '%weather%'           THEN 'esg_climate'

    WHEN pf.failure_mode LIKE '%oecd%'              THEN 'international_markets'
    WHEN pf.failure_mode LIKE '%non_us%'            THEN 'international_markets'
    WHEN pf.failure_mode LIKE '%foreign%'           THEN 'international_markets'
    WHEN pf.failure_mode LIKE '%emerging_market%'   THEN 'international_markets'
    WHEN pf.failure_mode LIKE '%global%'            THEN 'international_markets'

    WHEN pf.failure_mode LIKE '%crypto%'            THEN 'non_equity_asset_classes'
    WHEN pf.failure_mode LIKE '%fx%'                THEN 'non_equity_asset_classes'
    WHEN pf.failure_mode LIKE '%futures%'           THEN 'non_equity_asset_classes'
    WHEN pf.failure_mode LIKE '%commodit%'          THEN 'non_equity_asset_classes'
    WHEN pf.failure_mode LIKE '%bond%'              THEN 'non_equity_asset_classes'

    WHEN pf.failure_mode LIKE '%analyst%'           THEN 'analyst_estimates'
    WHEN pf.failure_mode LIKE '%consensus%'         THEN 'analyst_estimates'
    WHEN pf.failure_mode LIKE '%estimate%'          THEN 'analyst_estimates'

    WHEN pf.failure_mode LIKE '%short_interest%'    THEN 'ownership_positioning'
    WHEN pf.failure_mode LIKE '%borrow_fee%'        THEN 'ownership_positioning'
    WHEN pf.failure_mode LIKE '%13f%'               THEN 'ownership_positioning'
    WHEN pf.failure_mode LIKE '%holdings%'          THEN 'ownership_positioning'
    WHEN pf.failure_mode LIKE '%institutional%'     THEN 'ownership_positioning'

    WHEN pf.failure_mode LIKE '%satellite%'         THEN 'alternative_data'
    WHEN pf.failure_mode LIKE '%credit_card%'       THEN 'alternative_data'
    WHEN pf.failure_mode LIKE '%geolocation%'       THEN 'alternative_data'
    WHEN pf.failure_mode LIKE '%web_scrape%'        THEN 'alternative_data'
    WHEN pf.failure_mode LIKE '%audio%'             THEN 'alternative_data'
    WHEN pf.failure_mode LIKE '%alt_data%'          THEN 'alternative_data'

    WHEN pf.failure_mode LIKE 'data_unavailable:%'
      THEN 'other_data'

    ELSE NULL
  END AS data_category
FROM paper_failure_modes pf;

-- 3. The headline aggregation: how many distinct papers would unlock if
-- `feature_name` becomes available. Only counts data-related failure modes.
CREATE OR REPLACE VIEW missing_data_demand AS
SELECT
  data_category,
  feature_name,
  COUNT(DISTINCT paper_id)    AS blocked_papers,
  array_agg(DISTINCT paper_id)  AS paper_ids
FROM missing_data_features
WHERE data_category IS NOT NULL
  AND feature_name  IS NOT NULL
GROUP BY data_category, feature_name
ORDER BY blocked_papers DESC;

-- 4. Category rollup for executive recommendations.
CREATE OR REPLACE VIEW missing_data_category_demand AS
SELECT
  data_category,
  COUNT(DISTINCT paper_id) AS blocked_papers,
  COUNT(DISTINCT feature_name) AS distinct_features,
  array_agg(DISTINCT feature_name ORDER BY feature_name) AS features
FROM missing_data_features
WHERE data_category IS NOT NULL
GROUP BY data_category
ORDER BY blocked_papers DESC;

-- 5. Provider-recommendation mapping. Static lookup for generating reports.
-- Maintained as a plain table (not a view) so operators can add rows without
-- migrations as new providers are evaluated. Seeded with the user's explicit
-- plans: FMP expansion + Massive (options) + "other unique data providers".
CREATE TABLE IF NOT EXISTS data_provider_recommendations (
  data_category        TEXT PRIMARY KEY,
  suggested_providers  TEXT[] NOT NULL,
  est_monthly_cost_usd NUMERIC,
  notes                TEXT,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO data_provider_recommendations
  (data_category, suggested_providers, est_monthly_cost_usd, notes)
VALUES
  ('alternative_data',        ARRAY['RavenPack','AlphaSense','Quandl Alternative'], 2500, 'Sentiment, news, satellite — high cost, niche unlocks. Evaluate per-paper ROI before committing.'),
  ('international_markets',   ARRAY['FMP Global','EOD Historical Data','Refinitiv Datastream'], 500, 'FMP has international tier; EOD is the cheapest broad coverage.'),
  ('intraday_market_data',    ARRAY['Polygon.io full intraday','Databento','Massive intraday'], 800, 'Polygon already on stack — upgrading tier adds minute/second bars. Massive is options-only.'),
  ('options_microstructure',  ARRAY['OptionMetrics','CBOE DataShop','Polygon options tick'], 1500, 'OptionMetrics IvyDB is academic standard. CBOE is cheaper for modern data.'),
  ('factor_library',          ARRAY['WRDS','AQR datasets','Ken French library','FMP ratios expansion'], 400, 'AQR and Ken French are free. WRDS subscription unlocks Compustat-linked factor panels.'),
  ('text_sentiment',          ARRAY['Tavily upgrade','RavenPack','AlphaSense','Bloomberg BN API'], 1200, 'Tavily already on stack — upgrading the plan unlocks full-text filings + transcripts.'),
  ('analyst_estimates',       ARRAY['FMP estimates tier','Refinitiv IBES','FactSet Estimates'], 600, 'FMP has an estimates add-on; cheaper than Refinitiv but shallower history.'),
  ('ownership_positioning',   ARRAY['FMP institutional','Whale Wisdom','S3 Partners (short interest)'], 450, '13F data via FMP; borrow/short data from S3 is the gold standard.'),
  ('non_equity_asset_classes',ARRAY['FMP crypto/fx','Alpha Vantage crypto','CME Datamine'], 300, 'Modest cost for broader asset-class coverage.'),
  ('esg_climate',             ARRAY['MSCI ESG','Sustainalytics','Trucost carbon'], 2000, 'ESG is expensive and largely subjective. Only invest if 3+ papers queue behind it.'),
  ('other_data',              ARRAY['TBD — evaluate per-paper'], NULL, 'Uncategorised unique-data requests. Reviewer should re-classify into existing categories.')
ON CONFLICT (data_category) DO NOTHING;
