-- Structured per-run signal aggregates. Replaces the brittle pattern of
-- regex-parsing workspaces/default/memory/signal_patterns.md (see incident
-- 2026-04-21 where check_signal_quality matched the *first* avgEV in the
-- file — the oldest entry — instead of today's).
--
-- research_report.py writes one row per pipeline cycle; pipeline_orchestrator
-- reads the latest row for the gate decision.

CREATE TABLE IF NOT EXISTS daily_signal_summary (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_date        DATE NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  regime          TEXT,
  n_signals       INT  NOT NULL,
  ev_pos          INT  NOT NULL,
  ev_neg          INT  NOT NULL,
  avg_ev          NUMERIC,        -- fraction, e.g. -0.0146 for -1.46%
  avg_p_t1        NUMERIC,
  high_conv_count INT  NOT NULL DEFAULT 0,
  port_beta       NUMERIC,
  port_sharpe     NUMERIC,
  worst_dd        NUMERIC,
  overbought      TEXT[],
  metadata        JSONB
);

CREATE INDEX IF NOT EXISTS daily_signal_summary_run_date_idx
  ON daily_signal_summary (run_date DESC, created_at DESC);
