-- Phase 2C — dedup forensics on research_corpus.
-- Append-only: ADD COLUMN.  No data dropped.
--
-- The Jaccard headline-dedup helper at src/research/headline_dedup.py is
-- staged behind these columns; once a news/headline ingest path is wired
-- (currently every research_corpus row is paper-shaped, so dedup is
-- intentionally inert), the wiring will populate dedup_group_id on the
-- whole insert batch and dedup_dropped=TRUE on rows that lost the
-- Jaccard race.  Append-only invariant: dropped rows are still INSERTed
-- with the flag set, never DELETEd.

ALTER TABLE research_corpus
    ADD COLUMN IF NOT EXISTS dedup_group_id UUID,
    ADD COLUMN IF NOT EXISTS dedup_dropped BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_research_corpus_dedup_group ON research_corpus (dedup_group_id);
