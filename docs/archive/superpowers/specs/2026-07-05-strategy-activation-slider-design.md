# Strategy Activation Slider — design

**Status:** DESIGN — recommended defaults below are PENDING operator confirmation of 3 points
(trades guard / zero-regime handling / manual override). Implementation is deploy-gated and
live-affecting (changes which strategy-regimes are sized). Do not build until confirmed.

## Operator directive (2026-07-05, verbatim intent)
Replace direct dashboard-tile per-regime eligibility configuration with a single **strategy
activation slider** (default **0.5**). It "automatically keeps active strategy regimes by regime
sharpe ≥ 0.5 as strategies are adjusted and re-backtested weekly." Any strategy with ≥1 regime
Sharpe ≥ 0.5 stays activated (in those regimes). This supersedes the bulk F2-round-2 deprecation.

## Impact preview at 0.5 (corrected metrics, 45/49 sized; 4 pending re-bt count as dormant for now)
Active book **49 → 32** strategies (≥1 eligible regime); eligible regime-cells **151 → ~56**.
The min-trades guard is nearly free (55 vs 57 cells).

## Design
### 1. Threshold store (dashboard-controlled)
Single global `strategy_activation_min_sharpe` (default `0.5`) in `pipeline_config` (read each
derive; fail-safe to 0.5). A dashboard slider on the operator control-room (:7870) reads/writes
it — mirror the existing corr-adjusted conviction slider plumbing (`server.js` gates card +
`regime_sizer_params`), but this value is a single scalar, not per-regime.

### 2. Eligibility deriver — `src/backtest/activation_assigner.py`
For each strategy that has a **corrected** latest `primary_window` run, read its per-regime
`strategy_backtest_regimes` (via `run_id`) and upsert `strategy_regime_params.eligible` per
(strategy, regime):
```
eligible[r] = (sharpe[r] is not None AND sharpe[r] >= threshold AND trade_count[r] >= MIN_TRADES)
```
- **MIN_TRADES = 20** (RECOMMENDED — pending confirm; preview shows ~free). This is the sizer-read
  store (`strategy_regime_params`), NOT the manifest. Unlike the legacy `eligibility_assigner`
  (which writes the manifest and *refuses to wipe to empty*), this deriver is authoritative for
  the sizer and DOES set all-FALSE when no regime qualifies (operator wants auto-dormancy).
- **Zero-eligible-regime (RECOMMENDED pending confirm):** set all regimes eligible=FALSE →
  strategy is dormant (unsized) but registry status stays `approved`; a later re-backtest that
  lifts a regime ≥ threshold auto-reactivates it. (Alt options the operator may pick:
  auto-deprecate registry, or flag-for-review.)
- Emit a prior→new diff; post net activations/deactivations (esp. new dormancies) to #botjohn-log
  for observability (this is a live sizing change).
- CLI: `--all` / `--strategy-id` / `--dry-run` / `--min-sharpe` (default = the config value).

### 3. Wiring into the weekly cycle
Call the deriver **inside `weekly_live_sharpe.js`, BEFORE `strategy_weights --rebuild`**, so
eligibility is refreshed from the freshest corrected metrics every weekly cycle, then weights are
computed on the updated eligible set. Also runnable on-demand. **Initial run:** once the 4 stale
re-backtests finish, run `activation_assigner --all` at 0.5 on corrected metrics, capture the
eligibility diff, then the controlled reweight.

### 4. Dashboard — remove manual tiles (RECOMMENDED: fully automatic, pending confirm)
Retire the manual per-regime eligibility WRITE paths (`server.js` transition-upsert eligibility
portion ~1595, `routes_regime_eligibility.js` endpoints); make the eligibility view read-only
(derived) and add the activation slider. (Alt: keep an emergency per-cell manual "pin" the
deriver won't overwrite.)

## Interactions / coordination
- **Legacy `eligibility_assigner.py`** writes `manifest.metadata.eligible_regimes` (NOT sizer-read;
  the sizer reads `strategy_regime_params.eligible`). To avoid two competing eligibility notions,
  either retire it or have it read the same threshold; note as a follow-up in the plan.
- **Clamp cap** (bt_sharpe_plausibility_cap=5.0) is orthogonal — it bounds weighting, not eligibility.
- **Corrected-engine default** must be live (.env flags or code default) so weekly re-backtests keep
  producing corrected regime Sharpes the deriver reads — else eligibility drifts back on stale data.

## Non-goals
- Does NOT change the candidate→live promotion gate (that stays the class-aware floor; this is the
  per-regime SIZING eligibility for already-live strategies). Alignment can be a later decision.
- No master-data mutation; no migration (uses existing `strategy_regime_params.eligible` + a config key).

## Rollout
SDD (spec→plan→implement→review). Deploy operator-gated: johnbot restart (weekly_live_sharpe wiring
is a cron module, re-read on next run) + dashboard restart for the slider UI. Sequence WITH the
Phase 1e controlled reweight (derive eligibility → reweight, one coordinated live event, before/after captured).
