-- 091_pipeline_config_lambda_seed.sql
-- Seed the dashboard-tunable lambda parameter that governs
-- daily total notional deployed = lambda × NAV.
INSERT INTO pipeline_config (key, value, description)
VALUES (
  'position_sizing_lambda', '2.0',
  'Daily notional deployed = lambda × NAV. Range [0.10, 3.50]. Adjustable via dashboard.'
) ON CONFLICT (key) DO NOTHING;
