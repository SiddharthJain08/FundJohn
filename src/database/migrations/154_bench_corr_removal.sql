-- 154: benchmark-correlation removal threshold (operator directive 2026-09-04).
-- With the beta budget holding the market through SPY, an alpha name highly
-- correlated with the benchmark is redundant beta; the sizer removes it
-- outright (|corr| >= thr over the trailing 63d window) instead of leaving it
-- to the 20% cluster budget, which cannot see SPY (benchmark tickers are
-- excluded from clustering per spec 2026-08-29 D6). <=0 or >=1 disables.
-- Idempotent: never overwrites an operator-edited value.
INSERT INTO pipeline_config (key, value, description, updated_at)
VALUES ('benchmark_corr_removal_thr', '0.60',
        'Operator directive 2026-09-04: alpha tickers with |corr(t, benchmark)| >= this threshold (trailing 63d daily returns) are removed from the sized targets outright — held names orphan-close, conviction is not redirected. <=0 or >=1 disables. Kill switch: OPENCLAW_BENCH_CORR_REMOVAL=0.',
        NOW())
ON CONFLICT (key) DO NOTHING;
