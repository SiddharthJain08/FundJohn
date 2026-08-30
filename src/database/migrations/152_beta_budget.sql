-- 152: beta-budget sizing (2026-08-30; docs/specs/2026-08-30-beta-budget-sizing-spec.md D-4, D-6).
-- Idempotent: never overwrites an operator-edited value.
INSERT INTO pipeline_config (key, value, description, updated_at)
VALUES ('benchmark_max_nav_frac', '1.0',
        'Spec 2026-08-30 D-4: under OPENCLAW_BENCH_BETA_BUDGET=1 a benchmark ticker''s |target_usd| is clamped to this fraction of NAV (shaved, never redistributed). 1.0 = the reference portfolio is unlevered.',
        NOW())
ON CONFLICT (key) DO NOTHING;

INSERT INTO pipeline_config (key, value, description, updated_at)
VALUES ('bench_realized_anchor', '2026-06-23',
        'Spec 2026-08-30 D-6: anchor date (YYYY-MM-DD) for the daily bench_realized book-vs-SPY line appended to the #trade-reports digest. 2026-06-23 = start of the P&L-bleed window.',
        NOW())
ON CONFLICT (key) DO NOTHING;
