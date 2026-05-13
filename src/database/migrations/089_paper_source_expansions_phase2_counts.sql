-- 089_paper_source_expansions_phase2_counts.sql
-- Tier-2 fix from 2026-05-13 audit: paper_source_expansions tracks
-- `papers_imported` from paper_expansion_ingestor.js's own inserts, which
-- has been 0 since the 2026-04-25 prompt reframe (Opus now discovers feed
-- URLs, not papers). The real ingest happens downstream via Phase 2
-- (expanded_sources.py invoked from saturday_brain.js). Without a column
-- to capture that count, the operator-facing metric reads "0 imports per
-- $4 run" and looks broken.
--
-- This migration adds:
--   papers_imported_post_phase2 — count of new rows in research_corpus
--                                  attributable to expanded_sources.py
--                                  for this expansion's discovered feeds.
--   sources_already_seen        — count of discovered domains that were
--                                  already in our corpus (dedup signal).
--   notes                       — free-text post-run summary for operator.

ALTER TABLE paper_source_expansions
    ADD COLUMN IF NOT EXISTS papers_imported_post_phase2 INTEGER,
    ADD COLUMN IF NOT EXISTS sources_already_seen        INTEGER,
    ADD COLUMN IF NOT EXISTS notes                       TEXT;
