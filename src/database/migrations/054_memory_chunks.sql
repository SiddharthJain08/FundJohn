-- 054_memory_chunks.sql
-- pgvector-backed semantic retrieval for workspace memory.
--
-- Replaces the wholesale-injection pattern in src/agent/middleware/workspace-context.js:
-- before, every agent call concatenated all of memory/*.md (active_tasks,
-- fund_journal, regime_context, signal_patterns, trade_learnings) into the
-- system prompt. With this table, an embedder writes 1..N chunks per .md file
-- and the middleware retrieves only the top-K relevant chunks per query.
--
-- Embed model is recorded so we can re-embed when we change models without
-- mixing vector spaces. Filename + workspace identify the source file so we
-- can rebuild on file change.
--
-- This migration only creates the schema. The embed pipeline (writes) and the
-- retrieval helper (reads) live in src/agent/memory/embed.js and
-- src/agent/memory/retrieve.js respectively. Both are no-ops until pgvector
-- is enabled and rows exist.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memory_chunks (
    id              BIGSERIAL PRIMARY KEY,
    workspace       TEXT        NOT NULL,
    source_file     TEXT        NOT NULL,
    chunk_index     INTEGER     NOT NULL,
    chunk_text      TEXT        NOT NULL,
    note_type       TEXT,            -- frontmatter type when present (paper|strategy|position|...)
    tags            TEXT[],          -- frontmatter tags when present
    tickers         TEXT[],          -- extracted from frontmatter or body
    embedding       vector(1536),    -- text-embedding-3-small dim; change with care
    embed_model     TEXT        NOT NULL,
    char_count      INTEGER     NOT NULL,
    source_mtime    TIMESTAMPTZ NOT NULL,  -- mtime at embed time → invalidate when file changes
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (workspace, source_file, chunk_index, embed_model)
);

CREATE INDEX IF NOT EXISTS idx_memory_chunks_workspace_file ON memory_chunks(workspace, source_file);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_type           ON memory_chunks(note_type);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_tickers        ON memory_chunks USING GIN (tickers);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_tags           ON memory_chunks USING GIN (tags);

-- ANN index for cosine similarity. ivfflat lists tuned for ~10k rows; rebuild
-- with `REINDEX INDEX CONCURRENTLY` if memory grows past ~50k chunks.
CREATE INDEX IF NOT EXISTS idx_memory_chunks_embedding
    ON memory_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
