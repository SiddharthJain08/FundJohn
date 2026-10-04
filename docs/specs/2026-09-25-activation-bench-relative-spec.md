# Activation: benchmark-relative eligibility (replaces the activation slider) — SPEC (RULED 2026-09-25 19:2x UTC)

**Status:** APPLIED 2026-09-26 02:41Z (merge `0c4e5425`, `--no-ff` of `a574d43c`, pushed; apply
`PYTHONPATH=src python3 -m backtest.activation_assigner --all --notify` same run: 278 evaluated, 24
activated, 14 deactivated, 9 newly dormant; bench run `51b5b915`, vector LOW_VOL 0.9457 /
TRANSITIONING 0.4409 / HIGH_VOL 0.5326 / CRISIS 1.575, hysteresis 0.10; operator ack 2026-09-25
22:16Z of the dry-run `dryrun-2026-09-25.txt`). Ledger:
`.superpowers/sdd/2026-09-25-activation-bench-relative/progress.md`. Changelog entry:
`docs/archive/changelog.md` (2026-09-26 bullet).

**Operator directive (2026-09-25, verbatim intent):** "remove the activation slider entirely and instead activate in
regime if strategy sharpe is >= to beta_spy, so around the same in low vol but looser in transitioning/high vol and
tighter in crisis."

Supersedes `docs/archive/superpowers/specs/2026-07-05-strategy-activation-slider-design.md` §1 (threshold store) and the
`s >= threshold` leg of `activation_assigner.compute_eligible` (`src/backtest/activation_assigner.py:282-286`).

## 1. Rule
For every non-sleeve strategy with a latest `primary_window` run and per-regime rows (`strategy_backtest_regimes`, or the
chosen `universe_shrink_metrics` sleeves when present — unchanged):

```
eligible[r] = sharpe[r] > class.min_sharpe            # existing class gate, unchanged
              AND dd_leg_passes(dd[r], calmar[r])      # existing, unchanged
              AND trade_count[r] >= min_trades         # existing (100 today), unchanged
              AND sharpe[r] >= bench_sharpe[r]          # NEW — replaces `sharpe[r] >= slider`
```
`bench_sharpe[r]` = `S_beta_spy`'s `strategy_backtest_regimes.sharpe` for regime r from ITS latest `primary_window` run
(today, run 51b5b915 of 2026-09-11: LOW_VOL 0.95 / TRANSITIONING 0.44 / HIGH_VOL 0.53 / CRISIS 1.58). Same backtest
engine, same window kind, same target geometry as the strategy being judged — an apples-to-apples comparison. The
sizing quantity `S_m` (forward SPY excess Sharpe at `benchmark_horizon_days`: 0.81/0.42/0.49/1.54) is NOT used here.
Benchmark sleeves stay always-on (Amendment 1 D-D1, unchanged). Crypto strategies: see open point D.

## 2. Fail-safe
- If the sleeve run or a regime row is missing/None: use the LAST APPLIED vector persisted in `pipeline_config`
  (`strategy_activation_bench_sharpe`, JSON by regime, written on every successful apply); if none exists, use
  `DEFAULT_MIN_SHARPE = 0.5` for that regime and WARN. Never widen to "everything eligible".
- The re-apply trigger "slider row newer than last applied" becomes "sleeve primary run newer than last applied" (the
  `strategy_activation_last_applied` stamp gains `bench_sharpe` and `bench_run_id`).

## 3. Removal of the slider (12 files reference it; `grep -l strategy_activation_min_sharpe src`)
- Dashboard control-room card + its band/apply endpoints (`server.js` ×12 refs, `activation_preview.js`): remove the
  control; the preview endpoint shows the bench vector instead (read-only).
- `activation_assigner.py`, `activation_apply.py`, `pipeline_orchestrator.py`, `daily_cycle_node.js`,
  `lifecycle.py`, `regime_qualification.py`, `auto_approval.js`, `saturday_brain_finisher.js`, `staging_approver.js`,
  `promotion_service.js`: replace every read of the scalar with the vector; audit rows record the per-regime threshold
  actually used (`rule='qualifies(>0·classDD·trades)+bench_relative'`).
- `pipeline_config.strategy_activation_min_sharpe` row is left in place (append-only) but no longer read; a comment row
  is NOT added (no schema churn).

## 4. Impact preview (read-only, 2026-09-25 19:3x UTC; class gate approximated as sharpe>0 AND trades>=100)
| Regime | bench | eligible now (slider 1.0) | eligible under the rule | Δ |
|---|---|---|---|---|
| LOW_VOL | 0.95 | 11 | 14 | +4 / −1 |
| TRANSITIONING | 0.44 | 7 | 14 | +7 / −0 |
| HIGH_VOL | 0.53 | 11 | 23 | +12 / −0 |
| CRISIS | 1.58 | 29 | 15 | +0 / −14 |
The exact diff (with the real DD leg) comes from the assigner's own dry-run before the first apply.

## 5. Operator rulings (2026-09-25 19:2x UTC — "go with your recommendations on all five")
A. Comparator = `S_beta_spy`'s `strategy_backtest_regimes.sharpe` per regime from its latest `primary_window` run.
B. Hysteresis: a cell ACTIVATES at `sharpe[r] >= bench[r]` and DEACTIVATES only at `sharpe[r] < bench[r] − 0.10`
   (band `ACTIVATION_HYSTERESIS = 0.10`, stored with the vector in the last-applied stamp). A cell that is currently
   eligible and sits inside the band keeps its state; the class gate legs (>0, DD, trades) are NOT hysteretic — failing
   any of them deactivates immediately, as today.
C. `min_trades` stays 100 (today's applied value; read from the last-applied stamp, else the class default).
D. Crypto strategies use the SAME SPY vector for now. Their backtest regime rows are already keyed by the four canonical
   equity regimes (verified 09-25: S_btc_momentum / S_btc_halving_clock rows are CRISIS/HIGH_VOL/LOW_VOL/TRANSITIONING),
   so the comparison is mechanical. FUTURE (operator intent, not this build): add BTC as a benchmark ticker with its own
   benchmark sleeve, and evaluate crypto strategies against it on the CRYPTO regime structure (`crypto_regime_states`).
E. Rollout: assigner dry-run diff posted to #general → operator ack → single apply → weekly cron; weights rebuild AFTER
   the apply (activation → weights → daily_returns → similarity, 07-20 runbook ordering correction).

## 6. Out of scope
Sizing (`S_m`, rule C, beta budget) is untouched; the sleeve's own eligibility is untouched; the manifest is untouched.

## 7. Future
Ruling D (§5) judges crypto strategies against the SPY vector for now because their backtest regime
rows are already keyed by the four canonical equity regimes. Not built in this apply: add BTC as its
own benchmark ticker with a dedicated benchmark sleeve, and judge crypto strategies against THAT
vector on the crypto regime structure (`crypto_regime_states`) instead of the equity one. No code
change proposed here; this is an operator-intent placeholder for a future spec.

## 8. Amendment 1 — activation EXCESS over the benchmark (operator-ruled 2026-09-27 21:2x UTC) — **APPLIED 2026-09-27 23:11Z** (merge `30c3522b`; excess row seeded at 0; apply 0/0)
Operator: "change the activation slider to an excess sharpe over the spy benchmark. It should look the same in the dashboard
set to 0 for now to match recent developments with the ability to deactivate cells in the same way if the activation excess
is increased beyond 0."

Rule (replaces §1's last leg):
```
threshold[r] = bench[r] + excess            # excess = pipeline_config.strategy_activation_excess_sharpe (scalar, default 0.0)
activate    : sharpe[r] >= threshold[r]
deactivate  : sharpe[r] <  threshold[r] − ACTIVATION_HYSTERESIS (0.10)   # band unchanged, now relative to bench+excess
```
- `excess` is a single global scalar (not per regime), read at derive time from `pipeline_config`, fail-safe to 0.0 (missing,
  malformed or non-finite ⇒ 0.0 + WARN). Negative values are allowed (looser than the bench) but the dashboard clamps the
  control to [−1.0, +2.0] in 0.05 steps.
- Dashboard: the control returns to the position and look of the removed min-Sharpe slider, labelled "Activation excess over
  S_beta_spy (regime Sharpe)", default 0.00; the card keeps the read-only bench vector and now also prints the effective
  per-regime thresholds `bench + excess`. PUT `/api/config/activation-excess-sharpe` writes the row; GET returns
  `{excess, bench, thresholds, …}`. The old min-Sharpe PUT stays 410.
- Re-apply trigger: a newer `strategy_activation_excess_sharpe` row than the last-applied marker marks eligibility pending
  (restore the `SLIDER_KEYS` entry for the NEW key in `activation_apply.py`; the min-trades key stays). Raising the excess
  therefore deactivates cells on the next daily activation step exactly as the old slider did, with the band.
- Marker/audit: `strategy_activation_last_applied` gains `excess`; audit rows record `bench`, `excess`, `threshold`
  (= bench + excess) per regime; `rule = 'qualifies(>0·classDD·trades)+bench_relative+excess'`.
- At excess = 0.0 the rule is byte-identical to §1/§5 as applied on 2026-09-26 — the first apply after this amendment must
  produce 0 activated / 0 deactivated (pin with the live dry-run before merge).
- Preview endpoint (`POST /api/activation/dry-run`) accepts an optional `excess` override so the operator can preview a
  higher excess before saving it (read-only; never persists).

## 9. Amendment 2 — the PROMOTION gate is bench-aware (operator-ruled 2026-10-04 21:18 UTC: "yes, make the promotion gate bench-aware") — **APPLIED 2026-10-04** (branch `promo-bench`; opus review NEEDS FIXES → re-review CLEAN)

**Problem.** The candidate→live qualification gate (`src/lib/promotion_service.js`, policy 2026-07-13 v2) promotes on
per-regime `sharpe > 0` + sleeve DD ceiling + ≥100 trades, while activation (§1, §8) requires
`sharpe[r] ≥ bench[r] + excess`. A strategy in the gap is promoted "live" by `system:sunday-auto-approval`, activated in
no regime by the finale `activation_assigner --all`, and auto-demoted by the Monday 04:00Z weights run — every week
(`S_sparse_cca_mean_revert`: demoted 08-15, promoted 08-22, demoted 09-28, promoted 10-03 19:59Z on CRISIS Sharpe 0.827 vs
bench 1.575). No trading impact (`strategy_regime_params` governs sizing) but it pollutes lifecycle history, misreports
live counts and spends a promotion on a strategy that cannot trade. This supersedes, for the promotion gate only, the
2026-08-29 D1 ruling that SPY's regime Sharpe "sizes, never gates"; activation has gated on it since 2026-09-26.

**Rule.** A regime sleeve QUALIFIES for promotion iff it passes the three existing per-sleeve class gates AND
`sharpe[r] ≥ bench[r] + excess` — the activation ENTRY threshold (no hysteresis band: the −0.10 band is deactivate-only,
so a regime that qualifies here is always activatable by the assigner whether or not a prior eligibility row exists).
Promotion therefore implies ≥ 1 activatable regime.

- `bench` = `pipeline_config.strategy_activation_bench_sharpe` (the vector the assigner stamps on every apply — tier 2 of
  §2; the JS gate reads config only and never re-derives from the sleeve run). `excess` =
  `pipeline_config.strategy_activation_excess_sharpe`, parsed exactly like the assigner's `get_activation_excess`
  (finite float; missing / unparseable / non-finite ⇒ 0; no step snapping, no bounds — review ruling 2026-10-04: the
  dashboard clamp `clampExcessValue` snaps to 0.05 and would make the gate looser than activation for a hand-written row).
- Sleeve source = the assigner's (`_load_rows`): the CHOSEN `universe_shrink_metrics` sleeves of the run when they exist,
  else `strategy_backtest_regimes` — so the gate and activation judge the same numbers after a universe shrink.
- A primary run with no per-regime sleeves can never be activated, so with the gate on the legacy total-window fallback
  refuses (`no_backtest` when the sleeve lookup failed, `no_qualifying_regime` when it is empty) instead of promoting on
  totals.
- Fail-safe (mirrors §2 tier 3, never fail-open to 0): bench row missing / malformed / a regime absent or non-finite ⇒
  that regime's bench is `0.5` and a WARN is logged once per evaluation. A DB error reading the config ⇒ same fallback.
- Applies to BOTH gate paths: the automatic path (`computeQualifyingRegimes`, no regimes named) and the operator path
  with named `eligible_regimes` (`evaluatePromotionGate`); `force` remains the only override. New failed-gate kind
  `bench` (per-sleeve) / `bench:<REGIME>` (named path); the per-regime diag gains `bench` and `threshold`.
- Exempt: benchmark-sleeve strategies (`benchmark_sleeve: true`, i.e. `S_beta_spy`) — never judged against themselves.
- Crypto and option classes use the same SPY vector (operator ruling 2026-09-25 §5; a BTC benchmark is future work, §7).
- Kill switch `OPENCLAW_PROMOTION_BENCH_GATE=0` restores policy v2 exactly (default: ON).
- Only candidate→live is judged; currently-live strategies are not re-evaluated by this gate (activation + auto-demote
  already handle them).
- `lifecycle.py`'s python mirror judges caller-supplied totals only — comments updated, no behaviour change.

**Impact preview (read-only, 2026-10-04 21:2x UTC).** 8 non-quarantined candidates: promotable under v2 = 0, under this
amendment = 0 (bench CRISIS 1.575 / HIGH_VOL 0.5326 / LOW_VOL 0.9457 / TRANSITIONING 0.4409, excess 0). The amendment
changes nothing this week except that `S_sparse_cca_mean_revert`, once auto-demoted on Mon 10-05, is no longer
re-promoted on the next Sunday pass.

**Known residuals (accepted).** Tier-1 (sleeve run) vs tier-2 (config) bench drift above the 0.10 band between a sleeve
re-backtest and the next clean `--all` apply; an activation `strategy_activation_min_trades` set above the gate's 100;
hand-written excess rows in non-decimal notations (`0x10`, `1_000`) parse differently in JS and Python.
