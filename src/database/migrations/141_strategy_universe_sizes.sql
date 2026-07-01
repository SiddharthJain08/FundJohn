-- 141: per-strategy universe size N (breadth-weight factor input, 2026-07-01)
--
-- N = |UniverseResolver.resolve(strategy_id, today)| — the size of the ticker
-- universe the strategy's predicate acts on. Feeds the √(ln N) breadth factor
-- applied to the corr-adjusted cum-Sharpe weights (Grinold breadth: broader
-- universe → more weight). Regime-independent, so stored ONCE per strategy
-- (NOT denormalised across regimes). Refreshed out-of-band by
-- scripts/refresh_universe_sizes.py — deliberately DECOUPLED from the weekly
-- strategy_weights rebuild so a resolver failure can never break live sizing.
CREATE TABLE IF NOT EXISTS strategy_universe_sizes (
    strategy_id   TEXT PRIMARY KEY,
    universe_size INT  NOT NULL CHECK (universe_size >= 0),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
