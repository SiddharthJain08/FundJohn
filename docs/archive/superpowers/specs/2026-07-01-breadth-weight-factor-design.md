# √(ln N) Breadth Weight Factor for the Corr-Adjusted Cum-Sharpe — Design

**Date:** 2026-07-01
**Branch:** `feat/breadth-weight-factor` (off `feat/intraday-regime-15min-prefetch` @ `e77dce1`)
**Status:** BUILT + tested + real-data dry-run verified. Deployed INERT (flag default-OFF).
**Operator decision (2026-07-01):** direction = **multiply** (broader universe → MORE weight).

## 1. Intent

Bake a per-strategy breadth factor into the weights used by the correlation-adjusted
cumulative-Sharpe calc (the sole live conviction gate + sizing weight). A strategy
that acts on a broader universe N gets **more** weight:

    g(N) = √(ln N / ln N_anchor),   N_anchor = 500 (sp500 baseline)

Economic basis: Grinold's fundamental law of active management, IR = IC·√breadth —
more independent bets ⇒ more conviction. (The operator chose this direction over the
inverse multiple-testing "haircut"; see the AskUserQuestion 2026-07-01.)

This reuses the √(ln N) shape SP-7 Phase B B3 defined (`breadth_factor`), but applies
it to the **weights** rather than the (now-retired) legacy `min_cumulative_sharpe` floor.

## 2. N — the universe size (stored + refreshed by the system)

`N_i = |UniverseResolver.resolve(sid, today)|` — the size of the ticker universe the
strategy's predicate resolves to (default `sp500` ⇒ N≈503; wider predicates ⇒ up to
~5036 on the current snapshot). Regime-independent.

- **Storage:** `strategy_universe_sizes (strategy_id PK, universe_size, updated_at)` —
  migration 141. Stored ONCE per strategy (not denormalised across regimes).
- **Refresh:** `scripts/refresh_universe_sizes.py` (UPSERT), on a nightly timer
  (`openclaw-refresh-universe-sizes`). **Decoupled** from the weekly strategy_weights
  rebuild on purpose — a resolver failure must never break live sizing. Universes only
  move on adoption / snapshot growth, so ≤1-day lag is fine.

## 3. Application (scoped to the corr calc only)

`orthogonalization.breadth_weight_factor(N, anchor=500)` → g(N); fail-safe to 1.0 for
N≤1, missing/non-numeric N, or degenerate anchor.

In `regime_blended_sizer._sharpe_cadence_path`, behind default-OFF flag
**`OPENCLAW_STRATEGY_BREADTH_WEIGHT`**: the weights fed to `_corr_adjusted_maps`
(BOTH the gate `weight_by_strat` and the sizing `eff_weight_by_strat`) are scaled by
g(N_i) per strategy. Scoped to fresh dicts passed only to the corr calc — NOT the
global weight maps — so FOLD + bracket-leader picks stay on RAW conviction (mirrors
the `size_scalar` precedent). Missing N ⇒ factor 1.0 (strategy unchanged).

`load_current` is unchanged; the sizer loads N via `strategy_weights.load_universe_sizes()`
(only when the flag is on — OFF path adds no query and is byte-identical).

## 4. Coupling — floors must be re-tuned

Any breadth reweighting rescales S_adj. Real-data dry-run (06-30 carried set, LOW_VOL):
|S_adj| median 1.43→**1.68**, max 1.62→**1.90**, rec_floor 1.43→**1.68** (~+17%, driven
by the N=5036 strategies at factor 1.171). **Before flipping the flag on**, the operator
must re-tune the per-regime corr floors up ~17% (or read the new `dist`/`rec_floor` from
#botjohn-log). HIGH_VOL 2.0 / CRISIS 2.5 remain above the |S_adj| range (book stays held
in those regimes) — pre-existing calibration item, unchanged by this.

## 5. Rollout

1. Deploy INERT: migration 141 applied, N backfilled (62/62), code merged with the flag
   default-OFF → byte-identical (90 sizer tests green, OFF-path unchanged).
2. Operator re-tunes per-regime corr floors from the dry-run / #botjohn-log dist.
3. Flip `OPENCLAW_STRATEGY_BREADTH_WEIGHT=1` in `.env` + restart johnbot.
4. Rollback = unset the flag (byte-identical OFF) or git revert.

Tests: `tests/test_breadth_weight_factor.py` (6), `tests/test_breadth_weight_wiring.py`
(4, incl. flag-off symmetric / broad-upweights / equal-N symmetric / missing-N-unity).
