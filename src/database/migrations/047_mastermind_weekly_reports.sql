-- Migration 047: MastermindJohn weekly strategy-stack reports
--
-- Phase 3 of the 10am-cycle pipeline restructure. MastermindJohn (the
-- renamed CorpusCurator) runs twice weekly:
--   mode=corpus         — Saturday 10:00 ET, paper-curation flow (unchanged
--                         from the legacy openclaw-curator schedule).
--   mode=strategy-stack — Friday 20:00 ET, analyses the live+monitoring
--                         strategy stack over each strategy's lifetime:
--                         realised PnL, Sharpe, max drawdown, regime attribution,
--                         correlation. Posts a memo to #strategy-memos and
--                         exact sizing deltas to #position-recommendations.
--
-- trade_handoff_builder.py (daily 10am cycle) reads the latest
-- strategy-stack row and forwards its recommendations to TradeJohn so the
-- weekly sizing guidance flows into Monday's orders.

CREATE TABLE IF NOT EXISTS mastermind_weekly_reports (
  report_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_date        DATE NOT NULL,
  mode            TEXT NOT NULL CHECK (mode IN ('corpus','strategy-stack')),
  memo_md         TEXT,                     -- markdown memo posted to #strategy-memos
  recommendations JSONB,                    -- structured sizing deltas for #position-recommendations
  input_stats     JSONB,                    -- count of strategies/trades analysed, token usage
  cost_usd        NUMERIC,
  status          TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','failed','dry_run')),
  error           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mastermind_weekly_reports_mode_date
  ON mastermind_weekly_reports (mode, run_date DESC);

-- One "latest" record per mode, convenient view for trade_handoff_builder.
CREATE OR REPLACE VIEW mastermind_latest AS
  SELECT DISTINCT ON (mode)
         report_id, run_date, mode, memo_md, recommendations, input_stats,
         cost_usd, status, created_at
    FROM mastermind_weekly_reports
   WHERE status = 'ok'
   ORDER BY mode, run_date DESC, created_at DESC;
