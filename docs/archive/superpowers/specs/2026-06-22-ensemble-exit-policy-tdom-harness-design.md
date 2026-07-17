# Ensemble Exit Policy — T-DOM Test Harness (Design)

- **Date:** 2026-06-22
- **Status:** Design — awaiting user review before implementation
- **Branch / worktree:** `feat/ensemble-exit-tdom` @ `/root/openclaw/.claude/worktrees/ensemble-exit-tdom` (off live HEAD `5a63a2c`)
- **Source spec:** *Ensemble Exit Policy Specification v1.0* (user-provided), reference simulator reproduced verbatim at `/tmp/exit_sim.py`
- **Author assumption:** implementing agent will build the harness, run the Section-9 validation (esp. **T-DOM**), and report. **No live wiring** in this scope.

---

## 1. Goal & what "test" means

The user-provided spec generates a deterministic exit policy (one stop `a`, an ordered take ladder `{(b_k, frac_k, t_k)}`, one time-stop `T_exit`) for a multi-strategy directional position on one underlying, optimizing risk-adjusted growth per unit time net of cost.

The spec's **synthetic** harness (T-INV, T-COST, T-VOL, T-DECAY, T-COND) only proves the simulator is self-consistent. It is **already reproduced** in our environment — all 5 PASS, numbers match Section 14 to the digit (`/tmp/exit_sim.py`). It is **not** decision-grade: T-INV is an identity, and T-COST is near-tautological (cost is charged once per path and `a*` is floor-pinned in both arms, so it cannot actually penalize stop-out frequency the way its docstring claims).

The decision-grade test is **T-DOM (Section 9)**: on our real held-out backtest panel, the generated policy's growth `G` must beat **both** rejected baselines net of cost, with the **block-bootstrap CI lower bound of the growth difference strictly positive** (not merely a positive point estimate). This harness builds and runs T-DOM. That is the deliverable.

---

## 2. Decisions locked

### User-chosen (this session)
1. **Scope:** synthetic reproduction (done) **+ full T-DOM** on our data. **No live wiring.**
2. **`half_life` proxy:** **signal-autocorrelation half-life (primary)** + **`cadence_days` (sensitivity arm)**. Rationale: `half_life` drives the takes and time-stop (the parts that move); `cadence_days` is the realized holding period *under the incumbent exit policy*, so using it as the sole proxy contaminates T-DOM with the incumbent's behavior (circularity).
3. **Shorts:** **mirror the generator for shorts**, use all clusters (≈3,979), not long-only. Shorts run through the verified **Short-Position Delta v1.0** (`exit_sim_short.py`, deltas D1–D3): D1 direction-aware price mapping (`stop=entry−d·a`, `take=entry+d·b`), D2 per-bar carry on the drift, D3 decay baseline = carry. Reproduced in our env (T-SYM, T-CARRY PASS, match the delta's Section-6 evidence to the digit).
4. **Short carry (D2/D3):** **tiered assumption + carry=0 sensitivity.** We have no borrow-rate data (only a binary `easy_to_borrow` flag; no div yield), so carry is an explicit *assumption axis*, not a sourced input. Primary arm: flat borrow keyed on `easy_to_borrow` (GC ≈ 0.3%/yr vs HTB ≈ 5%/yr, converted to per-bar) + div estimate from corp-actions if cheap. Sensitivity arm: `carry=0` (shorts → pure mirrors). **The market-dynamics asymmetries A1–A4 (leverage-vol, skew/squeeze, recall, margin) are deferred** — unverified Phase 2, needing data we lack.

### Forced by data / spec (not optional)
4. **Bar = 1 trading day; `session_end` dropped.** Our `execution_signals` are daily-EOD, multi-day holds (median 1d, mean 5.8d, max 28d; `signal_date` is a `DATE`). The spec is written intraday; the faithful mapping is non-intraday (`session_bars = None`), so `T_exit = t_star` (edge-exhaustion only).
5. **Replay = daily-bar multi-day first-touch.** `combine_replay.py`'s 5-min same-day walk structurally cannot represent a multi-day time-stop; with mean holds of ~6 days it would never observe `t_star ≈ 20` bars. Daily OHLC high/low touch over the holding window is the only faithful engine.
6. **Levels computed deterministically (verified, not assumed).** The spec's own preamble (§13) and Section-14 evidence show `a*` pins at the noise-band floor `a = k_min·σ_eff·√E_tau` for strong ensembles (our reproduction confirms `a_mult=4.50` identical across T-COST arms). Step 0 verifies this on real clusters; if it holds we skip the per-cluster Monte Carlo entirely (removing all CPU/RAM contention with the running calendar sweep). Any weak ensemble that goes interior keeps a (cheap, bounded) MC fallback.

---

## 3. Input mapping (spec → our data)

| Spec input | Source | Notes |
|---|---|---|
| multi-strategy clusters | `execution_signals`, ≥2 strategies, same direction, same `coalesce(target_date,signal_date)`, since 2026-05-04 | ≈3,979 clusters (2,486 long / 1,493 short). **Filter `direction='FLAT'`.** |
| `sharpe` (per strategy) | `strategy_weights_by_regime.effective_sharpe` via `strategy_weights.load_current(regime)` | Annualized backtest blended w/ per-trade live (known unit caveat; it is what the live sizer uses). Regime = cluster's `regime_state` (fallback LOW_VOL). |
| `C` (Jaccard) | `strategy_similarity.load_groups(regime)['matrix']`, sliced to the co-firing subset | Real N×N, [0,1], symmetric, persisted (mig 123). **Caveat: it is Jaccard-co-firing *blended with return-correlation*, not pure Jaccard.** kappa>30 → spec fallback `w=normalize(S)`. |
| `sigma_underlying` | daily **ATR(20)** of the underlying as of entry, from sliced `prices.parquet` | `oxford_crabel.atr()`. No stored column; computed ad-hoc. |
| `half_life` (primary) | per-strategy **signal-autocorrelation half-life** derived from the strategy's daily return/signal series | `half_life = ln2 / (−ln ρ̂)` from an AR(1) fit on `strategy_backtest_trades`/`signal_pnl` daily returns; clamp to sane bounds. |
| `half_life` (sensitivity) | `strategy_weights_by_regime.cadence_days` | Second full run for sensitivity only. |
| `confidence` | `daily_weight` (= `effective_sharpe/√cadence_days`) | No numeric confidence field; `daily_weight` is the upstream sizing weight. Spec defaults to `inv(C)@S` anyway; `confidence` only feeds baseline (b) and optional weight mode. |
| `entry_price` | `execution_signals.entry_price` | Anchor (top-Sharpe constituent for the cluster). |
| `txn_cost` | round-trip cost in σ units (config; default from a realistic bps assumption × entry / ATR) | Same value across all policies. |
| `direction` | `execution_signals.direction` → ±1 (LONG/BUY/BUY_VOL=+1) | Mirror geometry for −1 via `exit_sim_short` (D1). |
| `carry_per_bar` (shorts) | tiered on `easy_to_borrow` (`ticker_metadata_snapshots` / `alpaca_tradable_universe`): GC ≈0.3%/yr, HTB ≈5%/yr → per-bar; + div est. (corp-actions, optional) | **Assumption, not sourced.** Same value across all 3 policies on a cluster; also charged in the realized replay (D2). `carry=0` sensitivity arm. |

`win_rate`, `avg_win`, `avg_loss` are declared spec inputs but **not used** by `generate_exit_policy`'s code path — so they are not required for T-DOM (we note this rather than materializing them).

---

## 4. Architecture (modular, each unit independently testable)

All under the worktree; analysis-only; imports live `src/execution/*` and reads `/root/openclaw/.env` + `/root/openclaw/data/...` by absolute path.

```
harness/
  exit_sim.py            # reference generator (verbatim from spec §13) — longs
  exit_sim_short.py      # verified short delta D1–D3 (verbatim) — shorts
  generator.py           # dispatch by direction (long→exit_sim, short→exit_sim_short); normalize diagnostics shape
  inputs.py              # cluster extraction + per-strategy input mapping (sharpe, C-slice, sigma, half_life, weights, carry)
  half_life.py          # autocorrelation half-life estimator (+ cadence passthrough)
  baselines.py           # baseline (a) min-stop+cumulative-takes; baseline (b) conf-weighted-ATR-blend; (info) current-live V2 — all direction-aware
  replay.py              # daily-bar multi-day first-touch engine (shared by all policies); charges per-bar carry on shorts
  growth.py              # per-trade R, τ; growth G (spec §4); block-bootstrap CI on ΔG
  prices.py              # sliced daily-OHLC loader (ticker+date filtered; never loads full 728MB panel)
  run_tdom.py            # orchestrator: clusters → 3 policies → replay → G → CI → report (JSON + markdown)
  tests/                 # pytest unit tests per module (TDD)
```

The generator dispatches on cluster direction: `+1` → `exit_sim.generate_exit_policy` (long), `−1` → `exit_sim_short.generate_exit_policy` (carry-aware short). `generator.py` normalizes the two diagnostics shapes into one `Policy`. The realized replay charges the same per-bar carry on short positions for every policy, so a policy that exits sooner (shorter τ) genuinely pays less borrow — the only channel by which carry moves ΔG.

**Unit contracts (what each does / inputs / outputs):**
- `inputs.extract_clusters(window_start) -> [Cluster]` — Cluster = {day, ticker, dir, entry, [Strategy], regime}.
- `inputs.build_context(cluster, half_life_mode) -> (List[Strategy], Context)` — spec dataclasses ready for `generate_exit_policy`.
- `half_life.autocorr_half_life(strategy_id, regime) -> float (bars)`.
- `baselines.min_stop_cumulative(cluster) -> Policy`, `baselines.conf_weighted_atr(cluster, atr) -> Policy`, `baselines.current_live_v2(cluster) -> Policy`.
- `replay.first_touch_multiday(policy, daily_bars, entry, dir) -> {R, tau, exit_kind, tranche_fills}`.
- `growth.G(trades) -> float`; `growth.bootstrap_delta(trades_A, trades_B, n_boot, block='day') -> (delta, lo, hi)`.

A `Policy` is the common shape `{stop_dist, takes:[(dist,frac)], time_stop_bars, dir}` so the replay engine is policy-agnostic — the only thing that varies across the three is the levels.

---

## 5. Data flow

```
execution_signals ──extract──> clusters ──map(sharpe,C,σ,half_life,w)──> (Strategy[], Context)
                                                                              │
                  ┌───────────────────────────────────────────────────────────┤
                  ▼                              ▼                              ▼
        ensemble: generate_exit_policy   baseline(a): min-stop+cum     baseline(b): conf-ATR
                  │                              │                              │
                  └──────────── each → Policy ───┴──────────────────────────────┘
                                              │
                 sliced daily OHLC ──> replay.first_touch_multiday (same engine, per policy)
                                              │
                                  per-trade {R, τ, exit_kind}
                                              │
                          growth.G per policy  +  block-bootstrap CI on ΔG
                                              │
                       T-DOM verdict (lo>0 vs BOTH baselines?) + report
```

---

## 6. T-DOM methodology (the gate)

- **Per-trade return** `R_i`: realized dir-signed PnL of the trade under that policy, in **σ units** (PnL fraction on entry ÷ σ_eff), matching the spec's `R = pnl/sigma`. Partial takes accumulate fraction-weighted; remainder marked at time-stop. Identical risk-capital normalization across all policies (sizing is upstream/out-of-scope and identical per cluster, so ΔG is invariant to the size choice).
- **Per-trade time** `τ_i`: bars to final close (last tranche / stop / time-stop), in days.
- **Growth** (spec §4, log form, φ=0.5): `G = mean_i[ ln(1 + φ·R_i) ] / mean_i[ τ_i ]`, with `1+φR` clipped at 1e-6.
- **All three policies scored on the SAME realized replay** (same price paths, same R/τ definitions). We do **not** mix the MC's internal `G` with the realized `G`.
- **CI:** stationary **block bootstrap by trading day** (resample day-blocks, recompute the full ratio `G` each resample, take `ΔG = G_ens − G_base`). Day-blocking absorbs same-day cross-ticker/cross-strategy correlation. Report `ΔG` point + 95% CI `[lo, hi]` for **both** baselines.
- **Adoption gate:** `lo > 0` against **both** min-stop+cumulative **and** conf-weighted-ATR. Report, never a single point estimate. Apply **deflated-Sharpe-style** caution when reporting (multiple configs compared).
- **Informational third comparison:** current live **V2** (min-stop + uncapped-sum-TP) — not part of the formal gate, but the operator ultimately cares whether to *replace the live method*, so we report `ΔG` vs V2 too.

---

## 7. Baseline definitions (faithful to spec §0.2 / §12)

- **(a) min-stop + cumulative-takes:** stop = `min_i(stop_pct_i)·entry` (tightest constituent); takes = each constituent's own `target_1`, each releasing equal/`w`-fraction; **no time-stop** (rides to the common horizon cap). 
- **(b) confidence-weighted-ATR-blend:** stop = `(Σ w_i·m_i)·ATR` where `m_i=(entry−stop_loss_i)/ATR` and `w_i=daily_weight` (normalized); take = Sharpe-weighted-mean of constituent targets (single tranche); time-stop = clock-only → common horizon cap. **Move-to-breakeven:** §12 marks this baseline as BE-using; v1 runs **without BE** to keep baselines clean/comparable, with a BE arm available as a sensitivity if results are close (documented, not silent).
- **Common horizon cap:** all policies (including the ensemble's `T_exit`) are additionally capped at a shared max-hold `H_max` (default 30 trading days = observed max hold) so "no time-stop" baselines terminate and the comparison is apples-to-apples. `H_max` is a logged config.

---

## 8. Validation plan (TDD)

Build each module test-first. Tests use synthetic fixtures (no DB/network) where possible; DB-touching tests are read-only and tiny.

1. **Reference parity:** `exit_sim.py` reproduces Section-14 numbers (done); `exit_sim_short.py` reproduces T-SYM/T-CARRY (done). `generator.py` dispatch returns the long policy unchanged for `d=+1` and the carry-aware mirror for `d=−1`; with `carry=0` the short policy equals the long mirror (T-SYM at the harness level). Replay charges carry: a short held N bars loses `N·|carry|` more than the same trade at `carry=0`.
2. **Step 0 floor-pin probe:** run `generate_exit_policy` on ~50 real clusters; assert/measure `a_mult` at the floor. Gates whether MC is needed. **Logged**, not assumed.
3. **`half_life.autocorr_half_life`:** recovers known half-life from a synthetic AR(1) series within tolerance; bounded output on degenerate input.
4. **`replay.first_touch_multiday`:** hand-checked fixtures — stop-only, take-only, stop-wins-on-tie, partial-take-then-time-stop, gap-through-level (fill at level, conservative), no-touch→mark-at-cap. **Sanity cross-check:** on trades whose horizon is one session, reproduce `combine_replay.py`'s outcome.
5. **`growth` + bootstrap:** `G` matches a hand computation on a tiny trade set; bootstrap CI brackets the point estimate; degenerate (all-equal) inputs give zero-width CI.
6. **`baselines`:** levels match hand computation for a 2–3 strategy cluster fixture.
7. **End-to-end smoke:** ~50-cluster dry run completes, emits report, memory stays bounded.

Adversarial review pass (independent agents) on: the growth/bootstrap math, the replay tie/gap/partial logic, the short-mirror correctness, and the input-mapping (esp. C-slice conditioning and half_life estimation).

---

## 9. Resource safety (do not disturb concurrent work)

- **Running calendar sweep** (`panel_one.py`, ~1.65 GB, 1 core) and **tomorrow 12:00 UTC C3 flip** (restarts johnbot, guards on free-mem/no-heavy-proc) must not be disturbed.
- Deterministic levels ⇒ **no per-cluster MC fan-out**. If MC fallback is ever needed it runs single-threaded, `nice -n 19`, bounded `mc_paths`.
- **Prices loaded sliced** (ticker + date filtered) — never the full 728 MB panel.
- Main checkout HEAD untouched (worktree isolation) ⇒ C3's `systemctl restart johnbot` still loads the intended live branch.
- All heavy steps `nice`d; harness completes well before 12:00 UTC tomorrow, and we avoid the C3 guard window regardless.

---

## 10. Out of scope

- Live wiring of the policy into the execution sizer (separate operator-gated change + restart).
- Position sizing (consumed as input).
- Materializing `win_rate`/`avg_win`/`avg_loss` (unused by the code path).
- Re-deriving signal generation or the backtest that produced per-strategy stats.
- Short-delta **Phase 2 (A1–A4)**: leverage-effect asymmetric vol, skew/squeeze jump term, recall hazard, margin-geometry stop cap. Specified in the delta but unverified and data-hungry; future work.

---

## 11. Open risks / caveats (carry into the report)

- **C is a blend, not pure Jaccard** — note in results; the spec's licensing argument (Jaccard ⇒ well-conditioned) is only approximately satisfied.
- **`effective_sharpe` unit mix** (annualized backtest + per-trade live) — pre-existing, shared by all policies, so it does not bias ΔG but does affect absolute `mu0`.
- **half_life estimator** depends on per-strategy return-series length; short live history → wide AR(1) error. Bounds + the cadence sensitivity arm hedge this.
- **Replay realism:** daily OHLC first-touch can't see intrabar path order (stop-wins-on-tie is the conservative convention, applied uniformly).
- **Survivorship / regime:** clusters carry their `regime_state`; if sparse we fall back to LOW_VOL, noted per cluster.
- **Selection:** report deflated-Sharpe-style caution given multiple configs (primary + sensitivity + 3 baselines).
- **Short carry is fabricated, not sourced.** No borrow-rate/div data exists; the tiered GC/HTB carry is an assumption. At realistic GC scale (~1e-5/bar) the carry effect is negligible and shorts ≈ mirrored longs, so on most easy-to-borrow names the short policy is effectively the long mirror. Carry only bites on genuine HTB names — exactly where A1–A4 (deferred) also matter. State this prominently in the short results; do not present the short verdict as if borrow were measured.

---

## 12. Deliverables

1. Tested harness on branch `feat/ensemble-exit-tdom` (analysis-only).
2. T-DOM result: `ΔG` + 95% block-bootstrap CI for ensemble vs each baseline, split **long / short / combined**, across arms = {half_life: autocorr | cadence} × {short carry: tiered | 0}, plus informational vs current-live V2.
3. A markdown report: verdict (does it dominate?), distributions/CIs (not point estimates), floor-pin finding, diagnostics (stopout/take-hit/hold distributions, kappa/fallback rates), and the caveats above.
4. Recommendation: adopt / reject / inconclusive — with the explicit note that adoption (live wiring) is a separate, out-of-scope, operator-gated step.
