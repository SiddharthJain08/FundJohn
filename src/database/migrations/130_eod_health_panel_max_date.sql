-- 130: eod_compute_health.panel_max_date (fix C, 2026-06-04)
--
-- Records the pre-proxy parquet panel max date the engine actually decided on.
-- The 06-02/06-03 EOD computes ran on close[T−1] with healthy=True — nothing
-- detected the stale panel (sp6 diagnosis 2026-06-04 §10). Additive only.

ALTER TABLE eod_compute_health ADD COLUMN IF NOT EXISTS panel_max_date DATE;
