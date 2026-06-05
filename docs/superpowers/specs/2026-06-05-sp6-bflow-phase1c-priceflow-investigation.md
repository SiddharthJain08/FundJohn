# SP-6 B-flow Phase 1c — Flow+Price Joint-Model Investigation & DRAFT Pre-Registration

Date: 2026-06-05. Status: **PARKED — operator decision 2026-06-05 (see §8 closure addendum).**
The §5 DRAFT pre-registration was **WITHDRAWN before locking**: the E1–E5 energy deep-dive
(`analysis/bflow_phase1c_energy_report.md`) adjudicated the energy construction in-sample as
**dominated** (not merely null) by the raw displacement object already accruing on the Phase-1b
OOS clock. §§1–4 and 6 remain valid hypothesis-generation synthesis; §5 is of record only.

> **EPISTEMIC STATUS (read first).** This investigation GENERATES hypotheses; it cannot TEST
> them. n ≈ 31 in-sample sessions on a calm tape, ~2-3 effective independent feature/horizon
> tests. NO in-sample configuration in this memo is "working." The output is a ranked candidate
> menu plus ONE draft pre-registration. The in-sample joint-model result is an **explicit NULL**
> (see §3); the draft prereg carries that null as its prior, by design — it registers a question,
> not a finding.

---

## 1. Problem framing

The operator's B-flow object is **a single entry per ticker-day, decided on a 1-minute
filtration, that must be filled by a hard 16:00 ET deadline (else forced EOD close)**. The order
set is fixed; only the *timing* of the one entry is the counterfactual versus the EOD-dump
benchmark. That is an **optimal-STOPPING problem with a weak signal and a forced deadline** — NOT
a scheduling/rate-control problem (we do not split or pace a parent order). This distinction
governs which literature transfers (see §2): rate-scheduling and limit-order-posting results are
structural background only; the act-or-be-dumped single stop is the object.

**The operator's price-confirm intuition, mapped to microstructure theory.** The operator asked:
after a flow cluster, confirm the price actually moved (lower, for a long) *before* entering. The
clean theoretical anchor is the **transient-impact / propagator** picture (Bouchaud-Gefen-Potters-
Wyart 2004):

- A reframe that is **load-bearing and must not be inverted**: the fade is **NOT** a bet that flow
  reverts. Signed order flow *persists* (Lillo-Farmer long memory, positive sign-autocorrelation).
  The only thing that mean-reverts is the flow-induced **price displacement** — the transient
  component of impact, which decays while flow itself continues.
- Parameter-free consequence: **a transient component can only revert if a displacement actually
  occurred.** Flow that hit the book but did not move price (absorbed by hidden liquidity / the
  compensating anti-correlated liquidity of Lillo-Farmer) has nothing transient to decay → there is
  nothing to fade. This is exactly the operator's "check the price actually moved," and it does NOT
  require any informed-trader story.
- The one refinement a richer model buys is an **upper guard**: if price moved *much more* than the
  flow's expected impact (an over-move), that excess is plausibly the informational/permanent
  component — and may *continue*, not revert (HDIM surprise channel; Boehmer et al. retail
  continuation). So in principle the joint condition improves the fade by excluding two failure
  modes — absorbed-no-displacement (lower guard) and informational-over-move (upper guard).

That is the *theory*. §3 reports what the in-sample data actually said about it, and §4-§5 rank the
candidate forms and pick the most defensible one to register — with the empirical null as its prior.

---

## 2. Literature map (verified citations only)

Only papers whose abstracts/pages the survey agents fetched from primary/source pages are listed.
Applicability judgments are reproduced from the surveys; strength is **not** upgraded.

### 2a. Optimal stopping with a signal (control object = a single stop)

| Ref (verified) | Finding | Applicability to our single-stop deadline |
|---|---|---|
| **Lehalle & Neuman (2019)**, *Incorporating signals into optimal trading*, Finance & Stochastics 23(2) (arXiv:1704.00847) | OU predictive signal in a Gatheral-type objective; explicit singular optimal **trading-rate**; signal tilts the rate, netted against impact (opposite directions). | INDIRECT / structural only. This is rate-SCHEDULING, not single-shot stopping — do **not** import its control object. Transferable piece: the signal-vs-impact tension and the "what a full model approximates" reference. Far more parameters than n≈31 supports. |
| **Cartea, Donnelly & Jaimungal (2018)**, *Enhancing trading strategies with order book signals*, AMF 25(1) | Volume-imbalance predicts the next market-order sign and the immediate price move; boosts profits via limit-order placement under an inventory penalty. | INDIRECT for control (LO posting, not stopping). DIRECTLY relevant empirically — closest published analogue to our OFI feature family. Corroborates Phase-1b: imbalance predictive content is REAL but **very short-lived**, which is exactly why a single well-timed entry has only a tiny edge window and small effect sizes. |
| **Leung & Li (2015)**, *Optimal Mean Reversion Trading with Transaction Costs and Stop-Loss Exit* (arXiv:1411.5062) | Optimal DOUBLE-stop for an OU spread; optimal ENTRY is a bounded interval above the stop-loss; faster reversion pulls entry/exit closer. | Canonical reference for the SHAPE of an OU entry rule = a threshold/interval on the signal. BUT it is PERPETUAL (time-invariant boundaries → no deadline behavior) and needs ~4-5 params. At IC≈−0.03 the entry interval is near-degenerate. Justifies a *static threshold form*, not deadline dynamics. |
| **Kitapbayev & Leung (2017)**, *Mean Reversion Trading with Sequential Deadlines and Transaction Costs* (arXiv:1707.03498) | FINITE-HORIZON OU-family double-stop with sequential deadlines; boundaries are explicitly **time-dependent** (functions of time-to-deadline), via nonlinear integral equations. | CLOSEST verified citation to our deadline structure. Boundaries collapse toward forced action as the deadline nears (standard American-stopping mechanism). Still a double-stop (entry+exit) and needs full OU calibration + horizon. Motivates a *monotonically collapsing time-varying threshold* IF a static one is registered first. |
| **Azze, D'Auria & García-Portugués (2024)**, *Optimal stopping of an Ornstein-Uhlenbeck bridge*, SPA 172 (arXiv:2110.13056) | Finite-horizon stop maximizing E[X_τ] of an OU **bridge** (pinned terminal); free boundary solves a Volterra integral equation and is generally **NON-monotone**; continuation region does **not** collapse at the terminal time. | **CAUTIONARY reference only — do NOT cite as support for a collapsing threshold.** Its objective is peak-capture before a *known terminal pin*, not forced action at a deadline, so its boundary behavior does not match ours. Included to show exact finite-horizon OU stopping is analytically heavy and that the clean perpetual closed form does not survive a finite horizon — an argument *against* the full free-boundary form at our n. (The collapse intuition for OUR setting comes from generic American forced-deadline theory, i.e. Kitapbayev-Leung, not from this paper.) |

### 2b. Flow, impact, and the price-confirmation mechanism (the joint-model spine)

| Ref (verified) | Finding | Applicability |
|---|---|---|
| **Cont, Kukanov & Stoikov (2014)**, *The Price Impact of Order Book Events*, JFE 12(1) (arXiv:1011.6402) | Over short intervals, price change is **linear in OFI**, slope ∝ 1/depth; robust across 50 stocks and time scales. | PARTIAL / horizon-mismatched: the relation is CONTEMPORANEOUS and sub-second-to-~10-min, descriptive impact, not a forward signal. Use only to JUSTIFY the displacement axis — it grounds `price ≈ β·OFI` as the fair-impact baseline against which realized displacement is compared. β is **depth/regime dependent → must be estimated, never assumed = 1**, and at n≈31 estimated ONCE globally, never per-session. |
| **Bouchaud, Gefen, Potters & Wyart (2004)**, *Fluctuations and response…* (arXiv:cond-mat/0307332) | Bare propagator: price = Σ past trade impacts × a **decaying** kernel G(t); random-walk prices = balance of long-range-correlated MOs (super-diffusion) and mean-reverting LOs (sub-diffusion). | DIRECTLY supports the fade and is the SOLID theoretical core. Parameter-free implication: only a displacement that *occurred* can revert → do not fade flow that produced no displacement. Grounds the operator's rule with no informed-trader story. |
| **Taranto, Bormetti, Bouchaud, Lillo & Tóth (2018)**, *Linear models… I. Propagators: Transient vs History-Dependent Impact* (arXiv:1602.02735) | TIM (predictable flow → reverting impact) vs HDIM (the **surprise/residual** of order sign impacts price ~permanently; predictable part transient). | SUPPORTS only the UPPER guard, and **as the model's CLAIM, cite cautiously** (the "surprise=permanent" reading came from a model summary, not a verbatim abstract). Does NOT flip the operator's rule. Adds a parameter (β) → belongs in a richer alternative, not the headline. |
| **Nagel (2012)**, *Evaporating Liquidity*, RFS 25(7) (NBER w17653) | Short-term reversal returns proxy returns to liquidity provision; expected returns / conditional Sharpe **spike with VIX**; small in calm tapes, large in turmoil. | MOST decision-relevant for go/no-go: explains WHY Phase-1b was WEAK — n≈31 calm sessions = structurally low reversal premium, so weak IC is expected even if the effect is real. A calm-tape null is **not** a refutation. Motivates a higher-vol window for future data — but we do NOT pre-register a regime split now (see §4, +DOF, n-starved). |
| **Boehmer, Jones, Zhang & Zhang (2021)**, *Tracking Retail Investor Activity*, JF 76(5) | Marketable RETAIL order imbalance predicts return **CONTINUATION** (~10 bps over the following week). | **GENUINE CONTRADICTION to the fade — surfaced, not buried.** If the minute-scale flow being faded is retail-dominated, it predicts continuation, not reversal → fading loses. Empirical face of the HDIM upper-guard. Horizon is weekly, so it is a caution about flow *composition*, not a same-horizon result. |
| **Lillo & Farmer (2004)**, *The Long Memory of the Efficient Market* (arXiv:cond-mat/0311053) | Signed flow has LONG MEMORY (sign autocorrelation power-law, H≈0.7); efficiency preserved by anti-correlated liquidity/size fluctuations. | CRITICAL disambiguation: "flow reverses" is WRONG — flow PERSISTS. The fade must be a bet that flow-induced **price displacement** reverts while flow persists. The compensating anti-correlated liquidity is the absorption mechanism behind the operator's "absorbed flow" case. |
| **Kang, Lin & Xiong (2022)**, *What Drives Intraday Reversal?…*, JEDC 136 (SSRN 3756630) | Attributes INTRADAY reversal to liquidity **oversupply** by uninformed (retail) providers — a price-pressure overshoot that reverts. | SUPPORTS the fade at the operator's actual HORIZON (intraday). Caveat: Chinese market/sample differs — corroborating mechanism, not a transportable estimate. |
| **Lehmann (1990)**, *Fads, Martingales, and Market Efficiency*, QJE 105(1) (+ volume-conditioned-reversal & iceberg strands) | Seminal WEEKLY reversal; reversals STRONGER after large volume (Conrad et al.); iceberg/hidden-liquidity detection is real but book-depth-heavy. | Lehmann horizon-distant (weekly) — cite only for the reversal-after-large-move lineage. Volume-conditioning is the closest precedent for the operator's "condition on a magnitude event." Iceberg/absorption detection is parameter-heavy and needs book depth → out of scope; realized displacement is its low-cost proxy. |

### 2c. Combining correlated signals (governs any multi-feature combiner)

| Ref (verified) | Finding | Applicability |
|---|---|---|
| **DeMiguel, Garlappi & Uppal (2009)**, *Optimal Versus Naive Diversification*, RFS 22(5) | Across 14 optimized models / 7 datasets, NONE consistently beats naive 1/N OOS; estimation error swamps the optimization gain (needs thousands of observations). | Backbone for "equal-weight is the robustness FLOOR." At n≈31 with IC sd≈0.07, estimating signal weights is precisely the regime where estimation error dominates → equal weighting of sign-aligned z-scored signals is the correct default. |
| **Kakushadze (2015)**, *Combining Alphas via Bounded Regression* (arXiv:1501.05381) | With few-history/many-alphas, sample covariance is unreliable and unconstrained regression overfits; bounds/PC-regression control it. | Confirms estimated-weight combiners are unstable at short history → defer any learned-weight combiner until more sessions exist. |
| **Bacry & Muzy (2013)** (arXiv:1301.1135) + **Anantha & Jain (2024)** (arXiv:2408.03594) | Multivariate Hawkes captures microstructure at **TICK / event-time**; sum-of-exponentials Hawkes kernels give the best OFI forecast — also on tick data. | HONEST assessment: Hawkes' value is intrinsically tick-scale self-excitation. At 1-minute bars a Hawkes intensity collapses toward an exponentially-weighted OFI count and adds little over the windowed `ofi_5`/`ofi_15` already in hand, while adding kernel params to fit. **Do NOT adopt Hawkes at minute granularity in Phase 1** — it is the Phase-2 TICK-scale estimator upgrade of the same object (see §4(iv)). |

**Honesty-on-fit caveat (from the surveys, preserved):** NONE of the verified papers is a pure
single-shot-entry-by-deadline problem. Lehalle-Neuman and Cartea-Donnelly-Jaimungal are
rate/limit scheduling; Leung-Li and Kitapbayev-Leung are double-stopping. Our problem is the
degenerate single-stop case that sits between these literatures and is solved verbatim by none of
them. **That gap is itself the argument for the fewest-parameter forms:** with no off-the-shelf
closed-form optimum to import, a hand-tuned multi-knob rule would be pure curve-fitting at n≈31.

---

## 3. Empirics summary — IN-SAMPLE, HYPOTHESIS-GENERATION ONLY

> Verbatim from the exploratory header: *"IN-SAMPLE EXPLORATORY — HYPOTHESIS GENERATION ONLY.
> Nothing here is a result; every cell was pre-enumerated; the menu is reported in full. Any
> promising cell is a candidate for the Phase-1c pre-registration and ~4-7 weeks of OOS accrual
> away from being testable."*

Universe = Phase-1b Test-A (cached `(ticker,session)`, 60-valid-bar floor, session ≤ 2026-06-02),
**n_sessions = 34**. Statistic = per-session pooled Spearman IC, across-session clustered
t = mean / (sd / √n_sessions), **session = the cluster**. Target set fixed once for the whole
menu: `ret_fwd_15`, `ret_fwd_30`, `ret_to_dump`. Menu = **90 cells** (A 6 + B 6 + C 12 + D 6 + E 6
= 36 base; F = (A+C)×3 regimes = 54). **Effective independent tests ≈ 2-3** (features/horizons
highly correlated). **Anchor reproduced: YES** (ofi_15→ret_to_dump t=−2.81 vs Phase-1b ref −2.81;
vwap_disp_30→ret_to_dump t=−2.15 vs −2.15).

**A. Anchor (unconditional).** All reversion-signed, as in Phase-1b.

| feature | ret_fwd_15 | ret_fwd_30 | ret_to_dump |
|---|---|---|---|
| ofi_15 | −0.026 (t=−3.24) | −0.032 (t=−2.79) | −0.037 (t=−2.81) |
| vwap_disp_30 | −0.030 (t=−2.82) | −0.042 (t=−2.78) | −0.038 (t=−2.15) |

**B. Residual displacement** (`disp_resid` = per-session pooled-OLS residual of `vwap_disp_30` on
`ofi_15`). *Does displacement carry content BEYOND contemporaneous flow?*

| feature | ret_fwd_15 | ret_fwd_30 | ret_to_dump |
|---|---|---|---|
| disp_resid | −0.015 (t=−1.32) | −0.026 (t=−1.94) | −0.019 (t=−1.08) |
| raw vwap_disp_30 | −0.030 (t=−2.82) | −0.042 (t=−2.78) | −0.038 (t=−2.15) |

→ In-sample answer **≈ NO**: residual ICs collapse from raw 2.15-2.82 to |t| ≤ 1.94 (sign-coherent
but weak). Displacement is mostly the flow signal. **Note this used PER-SESSION residualization
(31 hidden betas — the more flexible, more peeking-prone form) and STILL only reached |t| ≤ 1.94**,
so the registered single-global-β object inherits a null prior either way.

**C. Confirm conditioning** — IC(ofi_15) within sign(ofi_15)×sign(vwap_disp_30) quadrants
(`ret_to_dump` shown; full grid in the exploratory):

| quadrant | ret_fwd_15 | ret_fwd_30 | ret_to_dump | median n/session |
|---|---|---|---|---|
| flowDn_priceDn ("confirmed sell") | −0.016 (−1.54) | −0.010 (−0.97) | −0.014 (−1.63) | 7975 |
| flowDn_priceUp ("absorbed sell") | +0.011 (+0.74) | +0.024 (+1.59) | +0.026 (+1.49) | 3439 |
| flowUp_priceUp | −0.007 (−0.80) | −0.020 (−2.17) | −0.031 (−1.98) | 9931 |
| flowUp_priceDn | −0.012 (−0.69) | −0.025 (−1.62) | −0.026 (−1.36) | 3427 |

→ The operator's literal mechanism reads **NEGATIVE in-sample**: the "confirmed sell pressure"
quadrant (flowDn_priceDn) is **WEAKER** than unconditional ofi_15, and the absorbed-selling
quadrant flips positive. Conditioning on price confirmation does NOT strengthen reversion here.

**D/E (largest-|t| traps — NOT edges).** D `f_conf = ofi_15·1{signs agree}` reaches t=−3.28 on
ret_fwd_15 and E `burst_confirmed` reaches t=−3.04 on ret_to_dump. Both are **within noise of plain
ofi_15 (−3.24 / −2.81)** and are **order statistics over 90 correlated cells**, NOT improvements.
They are NOT flagged as candidates — headlining the largest |t| is exactly the overfitting the
epistemic rules forbid.

**F (regime slices).** Every A+C-per-regime slice (HIGH_VOL n=4, LOW_VOL n=10, TRANSITIONING n=16)
is a small-n researcher-DOF expansion; all t's are order statistics, none interpretable. The
HIGH_VOL flowDn_priceDn t=−4.71 is large purely because n=4 — explicitly NOT a finding.

### The honest in-sample headline (quote, do not soften)

> **The joint flow+price model is a NULL in-sample: no conditioned or interaction cell beats
> unconditional ofi_15.** Displacement carries little content beyond contemporaneous flow
> (disp_resid |t| ≤ 1.94); conditioning ofi_15 on price confirmation does not strengthen reversion
> (the confirmed-sell quadrant is weaker; the absorbed quadrant flips positive). **The single best
> prereg candidate remains the UNCONDITIONAL ofi_15 reversion signal from Phase-1b; joint-model
> elaborations add researcher DOF without in-sample lift.**

The unconditional signal is already covered by the Phase-1b OOS amendment and accruing. Phase-1c's
distinct job is to put the most defensible **joint** object on the leak-proof OOS clock so the
flow+price question is *adjudicated* rather than left as a survey hunch — registered with this null
as its explicit prior.

---

## 4. Candidate architectures, RANKED

Ranked by parameter count, which **is** the robustness axis at this n. Per-session IC sd ≈ 0.07 ⇒
SE(mean IC) = 0.07/√n; the Phase-1b/amendment full-sample bar |t| ≥ 3 needs mean IC ≥ 0.036 at
n = 34 (≈ 0.038 at n = 31); detection edge n ≈ (0.21/μ)². Every form is a HYPOTHESIS; none is in-sample-validated.

### (i) 1-parameter residual displacement — global β  ⟵ RECOMMENDED HEADLINE (§5)
- **Form:** `resid_disp = vwap_disp_30 − β·ofi_15`, a continuous single feature. Reversion sign
  (negative IC): displacement beyond fair contemporaneous impact is transient → reverts.
- **Params:** ONE (β). Estimated ONCE globally, pooled across in-sample sessions, **frozen before
  any OOS scoring. Per-session β is BANNED (= 31 hidden params; named as the anti-peeking guard).**
- **Why this over the operator's 2-condition gate:** it encodes the *identical* economics ("price
  displaced more than fair impact ⇒ absorption surprise ⇒ revert") with one continuous knob and NO
  gate, rather than two hard thresholds (a burst cut + a displacement cut). Grounded by
  Cont-Kukanov-Stoikov (the β·OFI fair-impact baseline) and Bouchaud/Taranto (residual ≈ transient
  impact, which reverts). Two of three literature surveys converged on it independently.
- **Data appetite:** 1 param fit OOS; at μ ≈ 0.04, n ≈ 28 to clear |t| ≥ 3 — i.e. ~the current
  sample size *if the lift is real*. Treat as OOS-only; the current cache is the FIT set.
- **In-sample prior: NULL.** The B-section residual (even with the more flexible per-session β)
  reached only |t| ≤ 1.94. This object is registered to *adjudicate*, not because it looks promising.
- **Failure modes:** (a) "residual is just vwap_disp relabeled" if β is small / depth high →
  guarded by the registered incremental-IC endpoint vs raw vwap_disp_30; (b) calm-tape null is
  inconclusive (Nagel), not disconfirming; (c) if minute flow is retail-dominated the residual could
  carry a CONTINUATION component (Boehmer) and the sign could attenuate or flip — reported, not
  assumed away.

### (ii) Operator's 2-condition frozen gate (flow burst + displacement confirm)
- **Form:** arm on a flow-burst threshold (e.g. top-tercile |ofi_15| with sign), then require
  `sign(displacement) = sign(flow)` and `|displacement| ≥ θ` before the (single) entry; else EOD.
- **Params:** ≥ 2 hard thresholds (burst cut + θ), plus the implicit horizon — the MOST researcher-
  DOF of the low-complexity forms, despite being the operator's literal idea. The C/D/E cells show
  conditioning is *weaker* than unconditional, so the gate has a negative in-sample prior AND more
  knobs. **De-prioritized:** the continuous residual (i) captures the same mechanism with one knob
  and no gate. Carry as a named alternative, not the headline.
- **Failure modes:** subsetting shrinks effective n hard; two thresholds invite tuning; in-sample
  read is adverse.

### (iii) Static signal-threshold stop, optionally with a deadline-collapsing boundary
- **Form A' (1 param):** at each minute act the first time the reversion-signed signal crosses a
  fixed favorable threshold s*; else EOD. Leung-Li degenerate single-stop limit. At IC ≈ −0.03 the
  option-value of waiting is tiny, so the optimal free boundary is near-flat and a single threshold
  captures ~all of the (small) edge.
- **Form B' (2 params):** make the threshold collapse toward 0 as 16:00 nears, `s*(t) = s0·g(τ)` —
  the qualitatively robust prediction of finite-horizon forced-deadline stopping (Kitapbayev-Leung
  time-dependent boundaries). **NOT** justified by the OU-bridge paper (Azze et al.), whose boundary
  is non-monotone and does not collapse — that paper is cautionary only.
- **Role:** this is the *policy wrapper* that turns a signal (i)/(ii) into an actual entry decision;
  it is the form to use IF a policy/Δbps readout is ever produced. Out of scope for the IC-only
  draft prereg in §5. Adopt Form B' only after Form A' is registered.

### (iv) Full OU free-boundary stop / minute-Hawkes — REFERENCE ONLY, NOT for Phase 1
- OU finite-horizon free boundary needs ~4-5+ params + a Volterra/integral-equation solve; the clean
  perpetual closed form does not survive the deadline, and the bridge result shows boundaries can be
  fragile/non-monotone. Carry only as the "what we are approximating" benchmark.
- **Hawkes is the PHASE-2 TICK-SCALE estimator upgrade of the SAME object**, not a Phase-1 addition.
  At 1-minute bars a Hawkes intensity is a smoothed/decay-weighted OFI count that adds little over
  `ofi_5`/`ofi_15` while adding kernel params — near-degenerate here. It belongs where the
  literature operates (tick / event time), i.e. Phase 2.

### Combiner note (governs anything multi-feature)
If a combiner over `{ofi_5, ofi_15, disp_resid}` is ever wanted, the **equal-weight z-scored
composite is the robustness floor** (DeMiguel-Garlappi-Uppal); a ridge/bounded learned-weight
combiner collapses toward equal weights at this n and should be DEFERRED until ~100-150 sessions
across varied tape exist (Kakushadze). Logistic/tree combiners need 10^4+ effective observations,
not 31 clustered sessions — explicitly not for Phase 1.

---

## 5. DRAFT Phase-1c pre-registration (NOT LOCKED — operator must bless)

> **DRAFT.** Committing this memo does NOT lock the prereg. The lock is a separate operator action.
> Until then, every constant below is a *proposal*. This registers a **question** carrying an
> explicit in-sample NULL prior (§3); it is not a claim that the object works.

**Object (single, fixed in advance):**
- Feature: `resid_disp = vwap_disp_30 − β · ofi_15` (architecture (i)).
- `ofi_15` is the windowed signed-flow measure already in use (the SINGLE flow choice; not scanned).
- **β estimated ONCE, globally, pooled across all in-sample (fit) sessions, then FROZEN before any
  scoring on the evaluation set. Per-session β is BANNED** and named the anti-peeking guard
  ("31 hidden params"). β is a single scalar regression coefficient of `vwap_disp_30` on `ofi_15`
  over the pooled fit data.

**Hypothesis (pre-stated):** `resid_disp` has a NEGATIVE forward-return IC at minute granularity
(price displaced beyond fair contemporaneous impact reverts; transient-impact prior). If
significance arrives with the OPPOSITE sign that is a first-class finding (continuation / Boehmer
composition), not a kill.

**Primary endpoint (single):** per-session pooled Spearman IC of `resid_disp` vs forward return at
ONE fixed horizon chosen in advance — **`ret_to_dump`** (the Phase-1b PRIMARY target, the
economically relevant deadline horizon) — then across-session mean IC and t = mean/(sd/√n_sessions).
**Session = the cluster. Never pooled SEs.**

**Fit / evaluation windows (mirror the Phase-1b amendment exactly):**
- **Fit window:** sessions ≤ 2026-06-02 (the in-sample cache; where β is estimated and frozen).
- **Headline OOS:** sessions ≥ 2026-06-08 (post-arming, physically leak-proof — the harness was
  built 2026-06-05).
- Sessions 2026-06-03..06-05 are a **labeled supplement, never headline** (post-cache, pre-arming).

**GO/NO-GO (MIRRORS the 1b amendment — do NOT invent a stricter OOS bar):**
- **GO-confirmed** when BOTH hold:
  - (i) the **FULL-sample** IC grid for `resid_disp` meets the Phase-1b §5-style bar, including the
    **coherence clause** — `resid_disp` reversion-signed with **|t| ≥ 3** on the primary target
    (`ret_to_dump`) AND reversion-signed with **|t| ≥ 2** on **≥ 2 of** the secondary horizons
    {`ret_fwd_15`, `ret_fwd_30`}. (Phase-1b's "a lone max-t survivor does not GO" rule, adapted to
    the single-feature case: a single |t| ≥ 3 on one horizon is precisely the lone-survivor pattern
    the coherence clause exists to block.); AND
  - (ii) the **OOS-only** slice with **n_oos_sessions ≥ 20** shows `resid_disp` reversion-signed
    with **|t| ≥ 2**.
- The OOS leg uses **|t| ≥ 2, not |t| ≥ 3** — identical to the 1b amendment and the only value
  decidable at n_oos ≈ 20. Writing |t| ≥ 3 on the OOS slice would be inconsistent with 1b and
  undecidable at this n.
- **Explicit n-cost statement (required up front):** `resid_disp` is a *residual* (a subset/derived
  object), so it carries strictly LESS independent signal than the raw feature and inherits the
  in-sample NULL prior. Subsetting/conditioning SHRINKS effective n and tightens the correlation
  across the ~2-3 effective independent tests (Lit-2 guard). **The bar is therefore HARDER to clear,
  not easier.** A null is inconclusive (calm tape, Nagel), not disconfirming; a positive on calm
  tape is suggestive, not validated, until replicated on more / higher-vol sessions.

**Secondary / descriptive (NOT go/no-go):**
- (a) **Incremental IC of `resid_disp` over raw `vwap_disp_30`** — the registered adjudicator of
  "does price displacement add anything beyond flow." Guards the (i)(a) failure mode.
- (b) IC of the equal-weight z-scored composite as a robustness benchmark.
- (c) The §3 quadrant ICs recomputed OOS (the operator's literal confirm mechanism), reported
  regardless of outcome — its in-sample prior is "conditioning does not help."

**Multiple comparisons:** ONE primary feature × ONE horizon is registered → no correction on the
primary. Any secondary set is noted as ~2-3 effective independent tests and Bonferroni-adjusted.

**Policy-delta discipline (ABSOLUTE):** the go/no-go is **IC-only** — rank correlation is immune to
the order-book direction-drift confound that discredited the Phase-1b Test-B +22bps number (which
decomposed +25 long / −18 short = midday→close drift harvest, NOT timing skill). The safest course
and the default here is that **NO policy/Δbps number appears in the recommended prereg.** If any bps
number is ever shown it MUST be reported **split long vs short in the same line**, or not shown.

**Timeline arithmetic:** from 2026-06-08, accruing trading sessions Mon-Fri, **n_oos ≥ 20 ≈ 4 weeks
→ ~early July 2026** before the OOS leg is decidable. Until then the question is OPEN. The weekly
re-run (Sat 13:00 UTC) may be inspected freely as the OOS slice grows; the GO-confirmed criteria
above are not to be re-tuned after the fact.

**Recommended DRAFT prereg, one line:**
*Register `resid_disp = vwap_disp_30 − β·ofi_15` (single global β, frozen; fit ≤ 2026-06-02) on
`ret_to_dump`; GO-confirmed = full-sample |t|≥3 AND OOS (n_oos≥20, ≥2026-06-08) reversion-signed
|t|≥2; IC-only; in-sample prior = NULL.*

---

## 6. The loudest line

**More conditions are not more robustness at n≈31 — every added threshold is a researcher degree of
freedom. This memo proposes; only pre-registered OOS accrual disposes.**

---

## 7. Open operator decisions

1. **Bless or amend the DRAFT prereg (§5).** It is NOT locked by this commit. Specifically:
   confirm the single feature (`resid_disp`, global β), the single horizon (`ret_to_dump`), the
   IC-only/no-policy-number stance, and that the OOS leg uses |t| ≥ 2 (mirroring 1b) not |t| ≥ 3.
   **Confirm the full-sample coherence clause** — §5 adapts Phase-1b's "lone max-t survivor does not
   GO" rule to the single-feature case (primary |t| ≥ 3 AND |t| ≥ 2 on ≥ 2 secondary horizons).
   This is a *harder* bar than a lone primary |t| ≥ 3; operator may keep it (recommended,
   anti-overfitting) or drop it. Alternative on the menu if a *joint gate* is preferred over a
   continuous residual: architecture (ii) — but it has more knobs and an adverse in-sample prior.
2. **Merge `feat/sp6-bflow-phase1-oracle` to harden the timers.** The four systemd units
   (`bflow-minbar-accrual.{service,timer}`, `bflow-weekly-rerun.{service,timer}`) currently point at
   THIS worktree path (`/root/.config/superpowers/worktrees/sp6-bflow-phase1-oracle`) via
   WorkingDirectory + PYTHONPATH + absolute ExecStart. The branch is additive/research-only (two new
   scripts, no live-path changes). Merging it into the live branch and repointing the units at
   `/root/openclaw` removes the dependency on this worktree continuing to exist on disk. Until then,
   deleting the worktree breaks nightly minute-bar accrual and the weekly re-run.

---

## 8. CLOSURE ADDENDUM (2026-06-05, operator decision: PARK)

The §5 DRAFT pre-registration is **withdrawn, not locked**. Basis — the pre-enumerated E1–E5
energy deep-dive (`/root/openclaw/analysis/bflow_phase1c_energy_{report.md,grid.parquet,m3.parquet}`,
worktree commits `104d396..b2fc140`, adversarial spec-compliance review APPROVED, anchors
reproduce Phase-1b exactly):

1. **The registered decisive comparison returned DOMINATED, not null.** Paired per-session
   IC(r) − IC(raw disp_15) on the causal global-β residual = +0.014 / +0.016 / +0.021
   (paired t = +2.16 / +2.73 / +3.08 across the three targets): residualizing displacement on
   OFI strictly *removes* reversion signal. The DRAFT's premise was "register a live question
   with a null prior"; the in-sample answer is "an object dominated by a simpler one already
   accruing." Registering it would spend the OOS clock on an answered question.
2. **Object-mismatch note (integrity):** the deep-dive's residual uses `disp_W` = trailing
   close-return (the pre-enumeration's literal definition); the §5 DRAFT registers the
   `vwap_disp_30` residual — different objects, an internal inconsistency between the two
   docs. The withdrawal does not rest on either alone: the vwap_disp_30-residual collapsed in
   the exploratory (|t| ≤ 1.94 with the *optimistic* per-session β) AND the disp_15-residual
   is dominated under the causal β. Both definitions point the same way.
3. **E5 discriminator — both registered hypotheses rejected.** Reversion concentrates in the
   UNDERSHOOT leg (ret_to_dump IC=−0.073, t=−3.28: undershoots catch up); the OVERSHOOT leg
   is NOT negative (+0.037, t=+1.91: overshoots do not pull back). H_energy fails
   (both-subsets-negative violated); H_absorption fails in reverse (it predicted the
   undershoot leg as the non-reverting one). **The asymmetry is recorded as a
   hypothesis-generation SEED only** — an order statistic over a ~60-cell grid at n=34
   calm-tape sessions; pursuable only via a FRESH pre-registration adjudicated on fresh OOS
   sessions, never by promoting this grid's cell.
4. **M3 discipline validated:** no z-threshold passes the both-legs-individually-positive
   bar; buy +37 bps / sell −36 bps at every z is the textbook session-drift signature — the
   bar caught exactly the sum-mirage that discredited Phase-1b Test-B.
5. **E2 caveat of record:** the fitted propagator amplitude (~1.2e-8) makes its residual
   numerically ≈ raw cumulative session return; its |t|≈2 forward-horizon cells read as plain
   intraday reversal, not a flow-ledger effect.

**Standing disposition:** the only dispositive test remains the Phase-1b OOS amendment —
unconditional `ofi_15` / `vwap_disp_30` reversion ICs on sessions ≥ 2026-06-08 (accrual timer
21:40 UTC nightly, weekly re-run Sat 13:00 UTC; n_oos ≥ 20 ≈ early July 2026). No new object
goes on the clock. §7.2 (merge the worktree branch to harden the timer units) remains an open
operator decision.

---

## Appendix — verification & integrity notes (from the survey agents)

- Every cited paper had its abstract/page pulled from a primary source. No fabricated references.
  SSRN 2668277 and the Leung-Li World Scientific book page 403'd but are corroborated by RePEc/arXiv
  mirrors. Azze et al. (2110.13056) is included as a **cautionary** reference, not as support.
- Taranto/HDIM "surprise = permanent" is the model's CLAIM (model-summary read, not a verbatim
  abstract) → attached only to the upper-guard refinement; the CORE fade rests on the solid
  Bouchaud-Gefen-Potters-Wyart 2004 propagator.
- Cont-Kukanov-Stoikov exact second-level interval grid / R² are paywalled — NOT fabricated; only
  the well-established "contemporaneous, short-interval, depth-scaled" character is asserted.
- Kakushadze-Yu 2016 ("How to Combine a Billion Alphas") was deliberately NOT cited for the
  collinearity claim — its abstract does not support it.
- Horizon seam, stated honestly: the strategy lives at 5/15/30-min; CKS/propagator are
  sub-second-to-~10-min and contemporaneous, Lehmann/Nagel/Boehmer are daily-to-weekly, Kang-Lin-
  Xiong (intraday) is the closest same-horizon support but a different market. The theoretical case
  is an extrapolation across a regime boundary — another reason the WEAK in-sample result must not
  be over-read in either direction.
- Empirics integrity: 90 cells pre-enumerated before running, menu reported in full, no cell
  selected post-hoc, no policy/Δbps number anywhere; the largest in-sample |t| is an order statistic.
