-- Single source of truth for dataset freshness. The collector and
-- pipeline_orchestrator have each had their own opinion of "current" and
-- silently diverged (see 2026-04-21 incident: price_data stuck at
-- 2026-04-17 while collector reported "all current — skipped").
--
-- `data_freshness` exposes per-dataset max_date and delta_days against the
-- last completed trading day (Mon–Fri, ignores holidays — good enough for
-- a staleness alarm, not a calendar source of truth).

CREATE OR REPLACE VIEW data_freshness AS
WITH last_trading_day AS (
  SELECT (
    CASE EXTRACT(DOW FROM CURRENT_DATE)
      WHEN 0 THEN CURRENT_DATE - INTERVAL '2 days'  -- Sunday → Friday
      WHEN 1 THEN CURRENT_DATE - INTERVAL '3 days'  -- Monday → Friday
      WHEN 6 THEN CURRENT_DATE - INTERVAL '1 day'   -- Saturday → Friday
      ELSE CURRENT_DATE - INTERVAL '1 day'
    END
  )::date AS expected_date
),
sources AS (
  SELECT 'price_data'   AS dataset, MAX(date)::date AS max_date, COUNT(*) AS row_count FROM price_data
  UNION ALL SELECT 'options_data', MAX(snapshot_date)::date, COUNT(*) FROM options_data
  UNION ALL SELECT 'macro_data',   MAX(date)::date, COUNT(*) FROM macro_data
  UNION ALL SELECT 'market_news',  MAX(published_at)::date, COUNT(*) FROM market_news
  UNION ALL SELECT 'snapshots',    MAX(snapshot_at)::date, COUNT(*) FROM snapshots
  UNION ALL SELECT 'signal_pnl',   MAX(pnl_date)::date, COUNT(*) FROM signal_pnl
)
SELECT
  s.dataset,
  s.max_date,
  l.expected_date,
  (l.expected_date - s.max_date)::int AS delta_days,
  s.row_count,
  CASE
    WHEN s.max_date IS NULL                    THEN 'empty'
    WHEN s.max_date >= l.expected_date         THEN 'current'
    WHEN (l.expected_date - s.max_date) <= 1   THEN 'fresh'
    WHEN (l.expected_date - s.max_date) <= 3   THEN 'stale'
    ELSE 'very_stale'
  END AS status
FROM sources s CROSS JOIN last_trading_day l
ORDER BY delta_days DESC NULLS FIRST, s.dataset;
