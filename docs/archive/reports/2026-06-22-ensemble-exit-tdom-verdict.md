# Ensemble Exit Policy — T-DOM Result (metric-corrected)

**Date:** 2026-06-22 · **Branch:** `feat/ensemble-exit-tdom` · **Window:** 2026-05-04 → 2026-06-22

## Verdict: ⚠️ Do NOT adopt — the policy does not dominate. But it is NOT the catastrophic loser the first pass suggested; against the baselines there is **no robust edge in either direction.**

On our real multi-strategy clusters, the Ensemble Exit Policy **fails the T-DOM gate** (no statistically significant dominance over the rejected baselines, on any segment, under any clean metric) — so it is **not adoption-justified**. But the strong "significantly worse" reading of the first pass was a **metric artifact**; the real per-trade gaps are small and their *sign* depends on the scoring/sizing convention. Net: no demonstrated reason to replace the simpler incumbent, and no evidence the spec's machinery is actively harmful.

> ⚠️ **Two corrections from review are baked into this verdict.** (1) The original "ΔG ≈ −0.12, significantly worse" was a log-clip artifact. (2) The first correction over-swung to "marginally favors the ensemble" — also not robust. The honest result sits between: **a null.**

## Why the metric matters (and which ones are valid)

Growth is `G = mean(score(R_i)) / mean(τ_i)`. Two choices: normalize `R` by **σ** (ATR units, equal-notional sizing) or by **risk capital** (= stop distance, equal-risk sizing); and score by **log** (Kelly) or **mean-variance** (MV). Each combination has a failure mode:

| Metric | Status | Failure mode |
|---|---|---|
| **risk-capital / log** | ✅ valid | — (full stop-out = −1, `1+φR>0` always) |
| **σ / mv** | ✅ valid | — (no log; bounded variance) — this is the **reference code's actual default** |
| σ / log | ❌ artifact | wide stops give `R≤−2` → `1+φR≤0` → **clipped to ln(1e-6)=−13.8**; penalizes the ensemble's wide floor-pinned stops 5× |
| risk-capital / mv | ❌ artifact | tight-stop baselines → huge `R` → variance term explodes (min-stop G = −7.2, live = −41.8) |

Key fact: **log-growth requires `R > −1/φ`**, which *only* risk-capital normalization guarantees — so σ/log is mathematically invalid for wide stops (this is also why the reference `exit_sim.py` got away with σ: it defaults to MV, not log). The verdict therefore rests on the **two valid metrics**: `risk-capital/log` and `σ/mv`.

**Clip-rate evidence for the σ/log artifact:** `clip_rate(σ)` = ensemble 0.046–0.054 vs baselines 0.000–0.016; `clip_rate(risk-capital)` = 0.000 for every policy.

## T-DOM under the two valid metrics — combined, n=413, 95% day-block bootstrap

| ensemble ΔG vs | risk-capital / log | σ / mv | Agree? |
|---|---|---|---|
| **min-stop + cumulative** | +0.008 [−0.024, +0.044] (tie) | **−0.030 [−0.048, −0.006] (sig. worse)** | ❌ sign flips |
| **conf-weighted-ATR** | +0.011 [−0.012, +0.036] (tie) | −0.010 [−0.031, +0.017] (tie) | ✅ no diff |
| current-live V2 *(info)* | +0.001 [−0.033, +0.039] (tie) | −0.017 [−0.036, +0.005] (tie) | ✅ no diff |

- **vs conf-ATR and live-V2:** no significant difference under *either* valid metric — a robust null.
- **vs min-stop (the simplest, tightest-stop baseline):** the metrics *disagree*. Under equal-risk/log it's a tie; under equal-notional/MV the ensemble is significantly worse by a small amount (−0.03). The disagreement is real economics — MV charges the extra **variance** wide stops add per unit notional; risk-capital/log does not — and the spec leaves the sizing convention upstream, so the sign is genuinely undetermined.
- **No metric, on any split, shows the ensemble's CI lower bound > 0 vs both gate baselines → T-DOM not passed.** (Long/short splits mirror combined: vs min-stop σ/mv is −0.028 / −0.038, both marginally significant; vs conf-ATR/live always straddle 0.)

For contrast the **flawed** σ/log reported ΔG vs min-stop = −0.12 [−0.18, −0.05]; the valid σ/mv gap is −0.03, ~4× smaller — most of the apparent underperformance was the clip.

## Mechanism

All policies post negative G in this ~7-week window (unfavorable to *every* exit policy). Scored validly, the ensemble's wider stop trades a lower stop-out *rate* for a larger loss-given-stop and adds per-notional variance; its decay-derived takes fill ~27% of the time. Against the strategies' own calibrated min-stop levels this nets to roughly even (equal-risk) or slightly behind (equal-notional). It neither beats nor is crushed by the baselines.

## Caveats

1. **Metric/sizing convention is load-bearing** — the sub-threshold sign vs min-stop flips between the two valid metrics. Report both; claim neither direction.
2. **`mc_paths` sensitivity (honest note):** levels are floor-pinned for ~75–80% of clusters but **not** mc-independent for the rest — the re-score (`mc_paths=2000`) gives ensemble σ/log = −0.168 vs the committed `mc_paths=20000` value −0.134 (baselines identical, they're deterministic). The gaps stay sub-threshold under both, so the *verdict* is unaffected, but absolute ensemble G carries ~±0.03 mc-noise. A definitive run would use `mc_paths≥20000`.
3. **Short carry fabricated** (no borrow data; tiered on `easy_to_borrow`; shorts ≈ mirrors at realistic scale). `C` is a Jaccard+return-corr blend, not pure Jaccard. `effective_sharpe` mixes annualized + per-trade (shared → no ΔG bias).
4. **800/3,924 strided clusters, 413 trades, one ~7-week all-negative window** — a single-window null, not a multi-regime verdict.

## Recommendation

**Do not wire live.** The policy shows no statistically significant edge over the simpler min-stop baseline or the current live method under any valid metric, and is mildly *behind* min-stop under equal-notional sizing — so there is no case to replace the incumbent on this evidence, and the spec's elaborate machinery does not earn its complexity here. It is also not disproven (the null is genuine). If pursued: a larger multi-regime backtest at `mc_paths≥20000`, scored under **both** risk-capital/log and σ/mv, with a fixed sizing convention chosen up front; adopt only if the lower bound clears 0 under both. Live wiring stays a separate, operator-gated step — not recommended now.

## Reproducibility

Tested harness (27 unit tests + adversarial review) on `feat/ensemble-exit-tdom`. Metric variants, clip rates, and both valid-metric bootstraps: `harness/out/rescore_autocorr_tiered.json` + per-trade dump `harness/out/trades_autocorr_tiered.json` (regenerate via `harness/rescore.py`). Original σ/log arms: `harness/out/tdom_autocorr_{tiered,zero}.json` (superseded for the verdict). Cadence half_life-sensitivity arms not run (non-decisive; `harness/finish.py`).
