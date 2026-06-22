# Ensemble Exit Policy — T-DOM Result (metric-corrected)

**Date:** 2026-06-22 · **Branch:** `feat/ensemble-exit-tdom` · **Window:** 2026-05-04 → 2026-06-22

## Verdict: ⚠️ INCONCLUSIVE — no significant difference. Do NOT adopt (no demonstrated edge), but the policy is NOT worse than the baselines.

On our real multi-strategy clusters, under the **spec-faithful growth metric**, the Ensemble Exit Policy is **statistically indistinguishable** from both rejected baselines and the current live method — every 95% bootstrap CI on `ΔG` straddles 0, with point estimates marginally favoring the ensemble. The spec's adoption gate (CI lower bound > 0 vs **both** baselines) is **not met**, so the policy is not adoption-justified by this test — but the correct reason is "no demonstrated edge," **not** "it underperforms."

> ⚠️ **Correction:** an earlier version of this report concluded "significantly worse / REJECT." That was a **metric artifact** (see below), caught on review. The corrected verdict is inconclusive.

## The metric artifact (why the first pass was wrong)

The first scoring used `R = pnl/σ` (ATR units) with the spec reference's log clip at `1e-6`. With **49% stop-outs** at the ensemble's **wide, floor-pinned** stops, `R ≈ −2` to `−5` there → `1+φR ≤ 0` → clipped to `ln(1e-6) = −13.8`, a ruin penalty applied hardest to the **widest-stop** policy. Spec §4 actually defines `R` as return on **risk capital** (= the stop distance), where a full stop-out is `R = −1` (`ln 0.5 = −0.69`, never clipped). The σ-shortcut is only valid when stops are ~equal across the compared policies — which they are not.

**Evidence it was an artifact:**
- `clip_rate(σ)`: ensemble **0.046–0.054** vs baselines **0.000–0.016** (≈5× more) — the ensemble alone trips the clip.
- `clip_rate(risk-capital)`: **0.000 for every policy** — the correct normalization removes the clipping.
- Tell: under σ/log the ensemble had the **worst** G despite the **2nd-best per-trade return** — economically incoherent.

## Results — growth G by metric (combined, n=413, 28 days)

| Metric | ensemble | min-stop | conf-ATR | live-V2 | Note |
|---|---|---|---|---|---|
| **risk-capital / log** ✅ | **−0.036** | −0.044 | −0.047 | −0.038 | spec-faithful, bounded → **use this** |
| σ / log (flawed) | −0.168 | −0.052 | −0.098 | −0.031 | clip artifact penalizes wide stops |
| σ / mv | −0.072 | −0.042 | −0.062 | −0.055 | σ-based |
| risk-capital / mv | −0.038 | −7.17 | −0.197 | −41.8 | opposite artifact: variance explodes on tight-stop baselines → unreliable |

Under the one clean metric (risk-capital / log) the ensemble is the **best point estimate**, but the bootstrap shows the gaps are not significant.

## T-DOM gate — risk-capital / log, 95% day-block bootstrap

| Split | ΔG vs min-stop | ΔG vs conf-ATR | ΔG vs live-V2 (info) | Gate |
|---|---|---|---|---|
| Combined (413) | +0.008 [−0.024, +0.044] p=0.67 | +0.011 [−0.012, +0.036] p=0.81 | +0.001 [−0.033, +0.039] | ❌ no strict dominance |
| Long (246) | +0.008 [−0.041, +0.049] | +0.008 [−0.019, +0.036] | −0.001 [−0.045, +0.040] | ❌ |
| Short (167) | +0.004 [−0.035, +0.057] | +0.010 [−0.021, +0.055] | +0.000 [−0.037, +0.056] | ❌ |

All CIs include 0 → ensemble ≈ baselines ≈ live, everywhere. For contrast, the **flawed** σ/log metric reported ΔG vs min-stop −0.12 [−0.18, −0.05] p=0.002 — the entire "significantly worse" signal lived in the clip.

## Why this is plausible (mechanism)

All policies post negative G in this ~7-week window (it was unfavorable to *every* exit policy). Once the wide-stop penalty is scored correctly (a 2σ stop-out is a −1 on risk capital, not ruin), the ensemble's profile — wider stop, fewer but not catastrophic stop-outs, decay-derived takes — nets out even with the baselines that use each strategy's own calibrated levels. No policy demonstrated an edge over another on this sample.

## Floor-pin finding (Step 0)

`frac_at_floor ≈ 0.75`, `frac_noiseband_bound ≈ 0.81` (median `a_mult` = 0.5 grid floor), `frac_at_ceiling ≈ 0.125`. Confirms the spec's claim that the stop is governed by the noise-band floor, not a cost-driven interior optimum — so per-cluster Monte Carlo is largely inert and deterministic levels are faithful. (This is also exactly *why* the σ-clip bit: floor-pinned ≈ wide stops.)

## Caveats (carry forward)

1. **Metric choice is load-bearing** — the verdict depends on scoring `R` as return on risk capital (spec §4), not σ. Both σ/log (clip) and risk-capital/mv (variance blow-up) are artifact-prone; **risk-capital/log is the only clean form** and is what the verdict rests on.
2. **Short carry is fabricated** (no borrow data; tiered on `easy_to_borrow`). Verdict is robust to it; at realistic scale shorts ≈ mirrored longs.
3. `C` is a Jaccard-co-firing **blend with return-correlation**, not pure Jaccard. `effective_sharpe` mixes annualized backtest + per-trade live (shared by all policies → no ΔG bias).
4. **800/3,924 strided clusters, 413 trades, one ~7-week all-negative window.** This is a single-window null result, not a multi-regime verdict. A larger / multi-regime sample could resolve the (currently insignificant) marginal edge either way.

## Recommendation

**Do not wire the policy live on this evidence** — it shows no demonstrated edge over the far simpler min-stop baseline or the current live method. But it is **not** inferior, so it is not disproven either. If pursued, the next step is a larger, multi-regime backtest (more clusters, more windows) scored under risk-capital/log; absent a positive lower-bound there, the simpler incumbent should stand. Live wiring remains a separate, operator-gated step and is **not** recommended now.

## Reproducibility

Tested harness (27 unit tests + adversarial review) on `feat/ensemble-exit-tdom`. Metric variants + clip rates: `harness/out/rescore_autocorr_tiered.json` (via `harness/rescore.py`). Original arm JSON: `harness/out/tdom_autocorr_{tiered,zero}.json` (σ/log — superseded for the verdict). Cadence half_life-sensitivity arms not run (non-decisive; `harness/finish.py`, run in a quiet window).
