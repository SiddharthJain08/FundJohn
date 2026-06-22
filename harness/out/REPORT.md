# Ensemble Exit Policy — T-DOM Result

**Date:** 2026-06-22 · **Branch:** `feat/ensemble-exit-tdom` · **Window:** 2026-05-04 → 2026-06-22

## Verdict: ❌ REJECT — the policy does NOT pass T-DOM

On our real multi-strategy `execution_signals` clusters, the Ensemble Exit Policy is **significantly worse** than the simplest rejected baseline (min-stop + cumulative-takes) and than the current live method, and is **not better** than the confidence-weighted-ATR baseline. The spec's adoption gate — bootstrap CI lower bound of `ΔG` > 0 vs **both** baselines — is not met on any segment. The result is **robust to the short-carry assumption**.

This is a decision-grade negative, not a harness artifact: the adversarial-review stage passed (2 findings fixed), 29 unit tests pass, and the mechanism is internally coherent (below).

## Method (recap)

Per the design doc (`docs/superpowers/specs/2026-06-22-...`) and plan: each cluster of ≥2 same-direction strategies on one underlying gets three exit policies — the spec ensemble (long via `exit_sim.py`, short via `exit_sim_short.py` deltas D1–D3), and the two rejected baselines — plus the current live V2 as an informational comparison. All are scored on the **same** daily-bar multi-day first-touch replay (stop-wins-on-tie, partial takes, time-stop, carry charged on shorts). Growth `G = mean_i[ln(1+φR_i)] / mean_i[τ_i]`, φ=0.5, R in σ units. CI via stationary **block bootstrap by trading day** (2000 resamples).

- Bar = 1 trading day; `session_end` dropped (non-intraday). σ_eff = daily ATR(20).
- Sample: **800 of 3,924 clusters** (strided to span all 31→28 distinct days — a logged memory/time bound, *not* silent truncation). After eligibility/ATR/slice drops → **413 trades** (skips: 369 few-legs-after-eligibility, 12 bad-slice, 6 no-ATR).

## Results

### Gate arm — autocorr half-life + tiered carry (n=413 trades, 28 days)

| Comparison | G(ensemble) | G(baseline) | ΔG | 95% CI | P(ΔG>0) | Pass |
|---|---|---|---|---|---|---|
| vs **min-stop + cumulative** | −0.134 | −0.052 | **−0.082** | [−0.138, −0.019] | 0.3% | ❌ |
| vs **conf-weighted-ATR** | −0.134 | −0.098 | −0.036 | [−0.093, +0.035] | 15.3% | ❌ |
| vs current-live V2 *(info)* | −0.134 | −0.031 | −0.103 | [−0.160, −0.037] | 0.2% | ❌ |

Long (n=246): ΔG vs min-stop −0.086, CI [−0.185, +0.004] (worse, not quite significant alone).
Short (n=167): ΔG vs min-stop −0.083, CI [−0.158, −0.013] (significantly worse).

### Carry-sensitivity arm — autocorr half-life + carry = 0 (n=413)

| Comparison | ΔG | 95% CI | P(ΔG>0) | Pass |
|---|---|---|---|---|
| vs min-stop + cumulative | −0.093 | [−0.148, −0.033] | 0.1% | ❌ |
| vs conf-weighted-ATR | −0.047 | [−0.105, +0.023] | 9.3% | ❌ |
| vs current-live V2 *(info)* | −0.114 | [−0.171, −0.050] | 0.1% | ❌ |

Carry barely moves the verdict (it only touches shorts, ~mirrors at realistic borrow). Tiered carry makes the ensemble's shorts *slightly less bad* (short ΔG −0.083 vs −0.108), consistent with carry shortening holds — but nowhere near a pass.

## Why it loses (mechanism, gate arm per-policy)

| Policy | mean R (ret/entry) | mean τ (days) | frac of takes filled | stop% / take% / time% |
|---|---|---|---|---|
| **ensemble** | +0.0069 | 4.41 | 0.27 | 49 / 27 / 24 |
| min-stop + cumulative | +0.0035 | 2.46 | 0.35 | 60 / 34 / 6 |
| conf-weighted-ATR | +0.0022 | 2.66 | 0.37 | 56 / 37 / 7 |
| current-live V2 | +0.0159 | 3.76 | 0.13 | 66 / 13 / 21 |

The ensemble holds longer behind a wide, floor-pinned stop, and its **decay-derived takes fill only 27%** of the time (they sit far out where real multi-day price rarely reaches), so it gives back more on the 49% that stop out. The baselines use each strategy's **own empirically-calibrated** stop/target levels and exit faster. Note all G are negative — this window was unfavorable to *every* exit policy — but T-DOM is about the **relative** ranking, and the ensemble ranks last/near-last everywhere. Interestingly live-V2 has the best per-trade R (uncapped TP lets winners run) at the cost of variance.

## Floor-pin finding (Step 0)

`frac_at_floor ≈ 0.75`, `frac_noiseband_bound ≈ 0.81` (median `a_mult` = 0.5, the grid floor), `frac_at_ceiling ≈ 0.125`. Confirms the spec's own claim: the stop is governed by the noise-band floor (A-5), not a cost-driven interior optimum — so the per-cluster Monte Carlo was largely inert and deterministic levels are a faithful shortcut for ~80% of clusters. (~12.5% hit the grid ceiling — strong, slow-decay ensembles wanting a very wide stop.)

## Caveats (read before acting)

1. **Short carry is fabricated, not sourced.** We have no borrow-rate/dividend data — only a binary `easy_to_borrow` flag. Tiered carry (GC ~0.3%/yr vs HTB ~5%/yr) is an assumption; at realistic GC scale shorts ≈ mirrored longs. Verdict is robust to it (both carry arms reject), but it is not measured.
2. **`C` is a Jaccard-co-firing blend with return-correlation**, not pure Jaccard (per `strategy_similarity`). Close to spec intent; kappa>30 fallback fired where ill-conditioned.
3. **`effective_sharpe` mixes annualized backtest + per-trade live** Sharpe — shared by all policies (doesn't bias ΔG) but inflates absolute `mu0`.
4. **800/3,924 strided subsample, 413 trades, one ~7-week window**, all-policies-negative regime. The ranking is consistent and CIs exclude 0 for the gate baseline, but this is one window — not a multi-regime, full-sample verdict.
5. **Selection:** four arms × three baselines compared; treat marginal (conf-ATR) results with deflation caution. The decisive results (vs min-stop, vs live-V2) are far from marginal.

## Deferred (non-decisive)

The **cadence half_life-sensitivity arms** were not completed — the orchestrator's detached recovery process (`harness/finish.py`) was stopped to protect the concurrent calendar sweep and box memory. Given the gate fails by a wide margin, is robust to the carry dimension, and most stops are floor-pinned (half_life mainly shifts the rarely-filled takes + time-stop), the cadence arm is very unlikely to flip REJECT. It can be completed cleanly later with `python3 harness/finish.py` (reduce `mc_paths` first; run in a quiet window).

## Reproducibility

Tested harness (29 unit tests, adversarial review) on branch `feat/ensemble-exit-tdom`. Per-arm JSON in `harness/out/tdom_autocorr_{tiered,zero}.json`, floor-pin in `harness/out/floor_pin.json`. **No live wiring** — that remains a separate, operator-gated step, and on this evidence it should not proceed.
