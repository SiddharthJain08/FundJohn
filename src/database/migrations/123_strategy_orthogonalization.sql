-- 123_strategy_orthogonalization.sql
-- Operational tables for the strategy orthogonalization engine (NOT master data —
-- versioned current/historical rows like strategy_weights_by_regime).
-- Spec: docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md

-- Per-strategy daily return series (reconstructed: differenced live marks + backtest).
CREATE TABLE IF NOT EXISTS strategy_daily_returns (
  id               BIGSERIAL PRIMARY KEY,
  strategy_id      TEXT NOT NULL,
  ret_date         DATE NOT NULL,
  daily_return_pct NUMERIC NOT NULL,   -- differenced daily delta (NOT cumulative level)
  regime_state     TEXT,
  source           TEXT NOT NULL,      -- 'live' | 'backtest'
  computed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (strategy_id, ret_date, source)
);
CREATE INDEX IF NOT EXISTS sdr_strategy_date_idx
  ON strategy_daily_returns (strategy_id, ret_date);

-- Per-regime strategy x strategy similarity matrix (JSONB blob; one current row per regime).
CREATE TABLE IF NOT EXISTS strategy_similarity_matrix (
  id            BIGSERIAL PRIMARY KEY,
  regime_state  TEXT NOT NULL,
  matrix        JSONB NOT NULL,        -- {strategy_id: {strategy_id: rho}}
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  trigger       TEXT NOT NULL,
  is_current    BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS ssm_regime_current_idx
  ON strategy_similarity_matrix (regime_state) WHERE is_current;

-- Tight cut -> fold-groups (near-identical). Representative = max effective_sharpe member.
CREATE TABLE IF NOT EXISTS strategy_fold_groups (
  id                BIGSERIAL PRIMARY KEY,
  regime_state      TEXT NOT NULL,
  group_id          INTEGER NOT NULL,
  strategy_id       TEXT NOT NULL,
  is_representative BOOLEAN NOT NULL DEFAULT FALSE,
  effective_sharpe  NUMERIC,
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  trigger           TEXT NOT NULL,
  is_current        BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS sfg_regime_current_idx
  ON strategy_fold_groups (regime_state) WHERE is_current;

-- Loose cut -> factor-blocks (same-factor family).
CREATE TABLE IF NOT EXISTS strategy_factor_blocks (
  id            BIGSERIAL PRIMARY KEY,
  regime_state  TEXT NOT NULL,
  block_id      INTEGER NOT NULL,
  strategy_id   TEXT NOT NULL,
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  trigger       TEXT NOT NULL,
  is_current    BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS sfb_regime_current_idx
  ON strategy_factor_blocks (regime_state) WHERE is_current;

-- Append-only audit of fold-group persistence (feeds the chronic-fold report + future Tier-3).
CREATE TABLE IF NOT EXISTS strategy_fold_audit (
  id              BIGSERIAL PRIMARY KEY,
  run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  regime_state    TEXT NOT NULL,
  group_id        INTEGER NOT NULL,
  strategy_ids    TEXT[] NOT NULL,
  representative  TEXT,
  member_sharpes  JSONB
);
