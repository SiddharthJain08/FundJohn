-- Phase 2-followup #17 — paper-fingerprint dedup on research_corpus.
-- Append-only: ADD COLUMN.  Existing rows (~6,002) get NULL fingerprints
-- (they're already in the corpus; we don't re-evaluate); future inserts
-- compute and check.
--
-- Catches the same paper appearing across multiple sources where
-- ON CONFLICT (source_url) DO NOTHING doesn't fire because the URLs differ
-- (e.g. an SSRN preprint also linked by a blog aggregator).
--
-- NOT a UNIQUE constraint — two rows with the same fingerprint can co-exist
-- (the FIRST insert AND the deduped-dropped second insert with
-- dedup_dropped=TRUE), mirroring the 2C dedup pattern (migration 097).
-- Helper: src/research/paper_fingerprint.py.

ALTER TABLE research_corpus
    ADD COLUMN IF NOT EXISTS paper_fingerprint TEXT;

CREATE INDEX IF NOT EXISTS idx_research_corpus_paper_fingerprint
    ON research_corpus (paper_fingerprint);
