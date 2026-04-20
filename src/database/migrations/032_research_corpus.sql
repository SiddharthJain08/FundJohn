-- Phase 1 of Opus Corpus Curator rollout.
-- Adds broad-ingest corpus and per-run curation history. The per-paper curated
-- candidates produced here feed research_candidates downstream (Phase 2 wiring).

CREATE TABLE IF NOT EXISTS research_corpus (
  paper_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source         TEXT NOT NULL,                  -- 'arxiv' | 'ssrn' | 'nber' | 'manual'
  source_url     TEXT UNIQUE NOT NULL,
  title          TEXT NOT NULL,
  abstract       TEXT NOT NULL DEFAULT '',
  authors        TEXT[],
  venue          TEXT,
  published_date DATE,
  keywords       TEXT[],
  raw_metadata   JSONB,
  ingested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS research_corpus_source_idx  ON research_corpus (source, ingested_at DESC);
CREATE INDEX IF NOT EXISTS research_corpus_pubdate_idx ON research_corpus (published_date DESC);

CREATE TABLE IF NOT EXISTS curator_runs (
  run_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at    TIMESTAMPTZ,
  model          TEXT NOT NULL,                  -- e.g. 'claude-opus-4-7'
  input_count    INTEGER NOT NULL DEFAULT 0,
  output_count   INTEGER NOT NULL DEFAULT 0,
  total_cost_usd NUMERIC(12,4) NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'running', -- 'running' | 'completed' | 'failed'
  run_metadata   JSONB
);
CREATE INDEX IF NOT EXISTS curator_runs_started_idx ON curator_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS curated_candidates (
  candidate_eval_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  paper_id                 UUID NOT NULL REFERENCES research_corpus(paper_id),
  run_id                   UUID NOT NULL REFERENCES curator_runs(run_id),
  confidence               NUMERIC(4,3) NOT NULL,  -- 0.000 - 1.000
  predicted_bucket         TEXT NOT NULL,          -- 'high' | 'med' | 'low' | 'reject'
  reasoning                TEXT,
  predicted_failure_modes  TEXT[],
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  queued_candidate_id      UUID REFERENCES research_candidates(candidate_id)
  -- Filled in only when a 'high' eval is promoted to research_candidates for PaperHunter.
);
CREATE INDEX IF NOT EXISTS curated_candidates_bucket_idx ON curated_candidates (predicted_bucket, confidence DESC);
CREATE INDEX IF NOT EXISTS curated_candidates_paper_idx  ON curated_candidates (paper_id);
CREATE INDEX IF NOT EXISTS curated_candidates_run_idx    ON curated_candidates (run_id);

-- Uniqueness guard: one eval per paper per run.
CREATE UNIQUE INDEX IF NOT EXISTS curated_candidates_paper_run_uniq
  ON curated_candidates (paper_id, run_id);
