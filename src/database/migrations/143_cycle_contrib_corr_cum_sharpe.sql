-- 143: persist the per-ticker corr-adjusted cumulative Sharpe (S_adj) each
-- sizing cycle (2026-07-14). The sizer computes gate_net_sharpe per ticker as
-- the SOLE conviction gate but previously discarded it (log-only). The
-- dashboard portfolio tiles surface it per position next to net contrib.

ALTER TABLE cycle_contributing_strategies
    ADD COLUMN IF NOT EXISTS corr_cum_sharpe NUMERIC;

COMMENT ON COLUMN cycle_contributing_strategies.corr_cum_sharpe IS
    'Signed corr-adjusted cumulative Sharpe (S_adj) for this ticker at sizing time — the conviction the position was taken/held at.';
