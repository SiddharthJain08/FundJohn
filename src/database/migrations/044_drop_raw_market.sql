-- Migration 044: drop raw market data tables (parquet-primary architecture)
--
-- Context: CHUNK D of the parquet-primary migration. Since CHUNK B (collector
-- writes to parquet) and CHUNK C (dashboard reads from parquet), these tables
-- have been dead weight. Dropping them reclaims ~406 MB of DB storage and
-- eliminates the duplicate source-of-truth for raw market data.
--
-- Safety: a full data-only parquet backup exists at
-- /root/openclaw/backups/raw_market_{DATE}/ covering all 1.6M rows.
--
-- Tables retained in DB (operational/transactional):
--   strategy_registry, execution_signals, signal_pnl, position_recommendations,
--   alpaca_submissions, market_regime, workspaces, trading_cycles,
--   data_coverage, collection_cycles, pipeline_runs, pipeline_config,
--   universe_config, market_news, verdict_cache, etc.

-- The data_freshness VIEW (migration 039) referenced these tables; parquet
-- freshness is now computed in code (src/pipeline/freshness.js).
DROP VIEW IF EXISTS data_freshness;

DROP TABLE IF EXISTS snapshots;
DROP TABLE IF EXISTS options_data        CASCADE;
DROP TABLE IF EXISTS fundamentals        CASCADE;
DROP TABLE IF EXISTS insider_transactions CASCADE;
DROP TABLE IF EXISTS macro_data          CASCADE;
DROP TABLE IF EXISTS price_data          CASCADE;
