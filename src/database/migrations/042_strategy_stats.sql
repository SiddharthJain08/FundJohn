-- Per-strategy aggregate view for the dashboard's Strategies page.
-- One row per strategy_id that has ever fired a signal. Strategies in
-- manifest.json with zero signals are handled by the /api/strategies
-- endpoint's left-join.
--
-- NOTE on units: signal_pnl.realized_pnl_pct / unrealized_pnl_pct are
-- stored as FRACTIONS (0.018 = 1.8%). The dashboard multiplies by 100
-- when rendering — we deliberately do NOT pre-scale here to stay
-- consistent with the existing /api/portfolio endpoints.

CREATE OR REPLACE VIEW strategy_stats AS
WITH sig_counts AS (
  SELECT
    strategy_id,
    COUNT(*)                                         AS total_count,
    COUNT(*) FILTER (WHERE status = 'open')::int     AS open_count,
    COUNT(*) FILTER (WHERE status = 'closed')::int   AS closed_count,
    MAX(signal_date)                                 AS last_signal_date,
    MODE() WITHIN GROUP (ORDER BY regime_state)      AS dominant_regime
  FROM execution_signals
  WHERE strategy_id IS NOT NULL
  GROUP BY strategy_id
),
pnl_stats AS (
  SELECT
    strategy_id,
    COUNT(*) FILTER (WHERE status = 'closed' AND realized_pnl_pct > 0)::int AS wins,
    COUNT(*) FILTER (WHERE status = 'closed' AND realized_pnl_pct <= 0)::int AS losses,
    ROUND(AVG(realized_pnl_pct) FILTER (WHERE status = 'closed')::numeric, 6) AS avg_realized_pct,
    ROUND(AVG(unrealized_pnl_pct) FILTER (WHERE status = 'open')::numeric, 6) AS avg_unrealized_pct,
    ROUND(MAX(realized_pnl_pct) FILTER (WHERE status = 'closed')::numeric, 6) AS best_trade_pct,
    ROUND(MIN(realized_pnl_pct) FILTER (WHERE status = 'closed')::numeric, 6) AS worst_trade_pct,
    ROUND(AVG(days_held) FILTER (WHERE status = 'closed')::numeric, 1)        AS avg_days_held
  FROM signal_pnl
  WHERE strategy_id IS NOT NULL
  GROUP BY strategy_id
)
SELECT
  s.strategy_id,
  s.total_count,
  s.open_count,
  s.closed_count,
  COALESCE(p.wins, 0)   AS wins,
  COALESCE(p.losses, 0) AS losses,
  CASE
    WHEN COALESCE(p.wins, 0) + COALESCE(p.losses, 0) = 0 THEN NULL
    ELSE ROUND(p.wins::numeric / NULLIF(p.wins + p.losses, 0), 3)
  END                         AS win_rate,
  p.avg_realized_pct,
  p.avg_unrealized_pct,
  p.best_trade_pct,
  p.worst_trade_pct,
  p.avg_days_held,
  s.last_signal_date,
  s.dominant_regime
FROM sig_counts s
LEFT JOIN pnl_stats p USING (strategy_id)
ORDER BY s.strategy_id;
