# SP-6 B-flow Phase 1b — HISTORICAL KILL-TEST (companion pre-registration)

Date: 2026-06-06. Status: **PRE-REGISTERED — committed before ANY historical
data fetch** (this commit's existence is the lock; the pull script refuses to
have run before it by construction: it ships in this same commit).

## 0. Relationship to the registered Phase-1b tests — ASYMMETRIC by design

This document does **NOT** amend the Phase-1b §5 verdict bar or the 2026-06-05
OOS amendment (`2026-06-05-sp6-bflow-phase1b-flow-predictability-prereg.md`).
Those stay frozen as written. This companion test:

- **CAN KILL** the minute-scale flow channel early (high-powered absence), but
- **CANNOT PASS it.** No historical outcome substitutes for the forward OOS
  bar (n_oos ≥ 20 sessions ≥ 2026-06-08 per the amendment). Rationale: the
  features, signs, and thresholds were chosen by researchers who knew 2026's
  market character; a backward pass is confounded by that knowledge in a way a
  forward pass is not. A kill is not so confounded — researcher bias inflates
  false positives, not false negatives, and the historical sample is ~22× the
  forward gate's power.

## 1. Why this test exists

In-sample Phase-1b was WEAK (best cell ofi_15 × ret_to_dump t = −2.81 on 34
sessions) and the forward gate resolves ~early July. History 2023→2026-03 is
~800 sessions of comparable microstructure: if the true effect were even a
third of the in-sample point estimate, |t| ≥ 3 is essentially guaranteed at
that n. Absence at that power is dispositive for the channel; presence is
merely encouraging.

**Window cap rationale (pre-committed):** reach-back is capped at 2023-01-03.
Probe evidence 2026-06-06: AAPL 14:30 minute bar = 636 trades (2017-06-07) vs
27,046 (2024-03-05). OFI/vwap-disp computed on sparse-trade bars measure a
different object; extending further back adds regime confound, not power.

## 2. Data (fetched ONLY after this commit)

- Source: Alpaca SIP 1-minute RTH bars (o/h/l/c/v/n/vw), fetched by the frozen
  `src/research/bflow/minbar_cache.py:get_session_bars` (chunk=20, 0.2s pacing,
  429/5xx retry) — the SAME code path the live nightly accrual uses.
- Cache: **NEW directory `data/cache/min_bars_hist/`**. The live accrual cache
  `data/cache/min_bars/` is never written by this test.
- Sessions: SPY dates in `data/master/prices.parquet` ∈ [2023-01-03,
  2026-03-31] — 813 candidate sessions. Disjoint from the in-sample cache
  (2026-04-13..2026-06-05) with an 8-session buffer.
- Universe: the frozen 505-ticker list `analysis/bflow_phase1b_hist/universe_505.txt`
  (sha256 `7b53c16068cf73be387d3cd48252f1350dfc369a9e54da2db1a00821cc99d26d`) =
  the union of non-sentinel tickers across the 37 in-sample cache files — the
  registered Test-A population definition applied to the cache. **Survivorship
  caveat (acknowledged, accepted):** this is today's worked set applied
  backward. For an execution-microstructure claim on liquid large-caps this is
  second-order; it would be fatal for an alpha claim and this test makes none.
- Adjustment: bars fetched RAW (same as live). Moot by construction — every
  registered feature and target is a within-session ratio; overnight
  split/dividend adjustments cannot enter any quantity.
- Tickers not yet listed (or delisted) on a historical session surface as
  zero-bar / sentinel rows — the registered 60-valid-bar floor excludes them
  per (ticker, session) exactly as in-sample.

## 3. Computation (frozen code, no new statistics)

- Harness: `python3 -m research.bflow.run_phase1b --cache-dir
  data/cache/min_bars_hist --analysis-dir analysis/bflow_phase1b_hist
  --oos-start 2027-01-01` at THIS commit. Features, targets, validity rules,
  and the Test-A statistic (per-session pooled Spearman → across-session mean,
  t = mean/(sd/√n_sessions), session = the cluster) are bit-identical to the
  registered design. Test A has zero fitted parameters.
- **Test B is INERT on history** (its intents are all 2026 worked-sessions →
  uniform no_bars against the hist cache). Its outputs, and the harness's
  printed `[bflow-p1b] VERDICT:` line (whose §5 logic folds Test B in), are
  **NOT read**. The ONLY object this test consumes is
  `analysis/bflow_phase1b_hist/bflow_phase1b_ic_grid.parquet`.
- Verdict computation: `scripts/bflow_phase1b_hist_evaluate.py` (committed
  herewith, mechanical, zero free parameters at eval time).

## 4. Eligibility + buckets (pre-committed)

- Eligible session: in-window AND at least one non-NaN PRIMARY cell (sessions
  with all-3 PRIMARY cells NaN — early closes, no minute≥385 dump window — are
  excluded WHOLESALE, secondaries included).
- **Data-quality floor: a KILL verdict is only valid with ≥ 700 eligible
  sessions.** Below that the run is INVALID-DATA (fix the pull; no verdict).
- Buckets: 2023H1, 2023H2, 2024H1, 2024H2, 2025H1, 2025H2, 2026Q1 (7 calendar
  buckets by session date). "Recent buckets" = {2025H2, 2026Q1}.

## 5. VERDICT RULES (pre-committed; sign-agnostic structure test)

PRIMARY cells = {ofi_5, ofi_15, vwap_disp_30} × ret_to_dump. Momentum sign is
first-class per the original prereg (§3: opposite-sign significance is
GO-inverted, not a kill) — so the kill rule demands no structure in EITHER
sign:

- **KILL** ⟺ BOTH:
  - (a) pooled (all eligible sessions): NO PRIMARY cell has |t| ≥ 3, AND
  - (b) recent buckets: NO PRIMARY cell has |t| ≥ 2 in EITHER of {2025H2,
    2026Q1}.
- **SURVIVE-AMBIGUOUS** ⟺ (a) holds but (b) fails — consistent with a
  recent-regime-specific effect; the forward gate proceeds exactly as
  registered, with no acceleration in either direction.
- **SURVIVE-STRONG[-INVERTED]** ⟺ pooled meets the §5 GO shape: ≥ 2 of 3
  PRIMARY cells |t| ≥ 3 with consistent sign, AND ≥ 2 secondary horizons with
  |t| ≥ 2 in the same sign. Suffix INVERTED if the sign is momentum (+).
  Effect: prior raised; the forward n≥20 bar is STILL REQUIRED for any GO.
- **SURVIVE-WEAK** ⟺ anything else.

Decision linkage: KILL ⇒ the minute-scale flow channel is closed and the July
forward decision is pre-empted (accrual timers may keep running for data
value). Any SURVIVE ⇒ status quo: the registered forward gate decides.

## 6. No-peek discipline

The pull script logs ticker/row/zero-bar counts ONLY. No IC, return, or
feature statistic is computed or printed before the evaluator runs; the
evaluator's verdict block is the first look at any historical IC.

## 7. Reporting

`analysis/bflow_phase1b_hist/killtest_verdict.md`: verdict + pooled grid +
per-bucket PRIMARY grids (mean IC, t, n) + eligibility/data-quality table.
Per-bucket sign-consistency is reported for operator interpretation but is
NOT part of the verdict rules above.
