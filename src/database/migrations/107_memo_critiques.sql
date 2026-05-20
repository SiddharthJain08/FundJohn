-- Migration 107: F3 — Mastermind self-critique loop tables.
-- strategy_memo_critiques  — three rows per (strategy_id, week_of) from Sonnet critics.
-- strategy_synthesis       — one row per (strategy_id, week_of) from Mastermind Opus synthesizer.

CREATE TABLE IF NOT EXISTS strategy_memo_critiques (
  id            BIGSERIAL PRIMARY KEY,
  strategy_id   TEXT NOT NULL,
  week_of       DATE NOT NULL,
  critic_role   TEXT NOT NULL CHECK (critic_role IN ('aggressive','conservative','neutral')),
  critique_text TEXT NOT NULL,
  cited_metrics JSONB,
  cost_usd      NUMERIC,
  duration_sec  NUMERIC,
  generated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(strategy_id, week_of, critic_role)
);

CREATE INDEX IF NOT EXISTS idx_critiques_strategy_week
    ON strategy_memo_critiques(strategy_id, week_of DESC);

CREATE TABLE IF NOT EXISTS strategy_synthesis (
  id                              BIGSERIAL PRIMARY KEY,
  strategy_id                     TEXT NOT NULL,
  week_of                         DATE NOT NULL,
  synthesizer_text                TEXT NOT NULL,
  original_recommended_size_pct   NUMERIC,
  adjusted_recommended_size_pct   NUMERIC,
  adjustment_reason               TEXT,
  critics_accepted                JSONB,
  critics_rejected                JSONB,
  cost_usd                        NUMERIC,
  generated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(strategy_id, week_of)
);

CREATE INDEX IF NOT EXISTS idx_synthesis_week ON strategy_synthesis(week_of DESC);
