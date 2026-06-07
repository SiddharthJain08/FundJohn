# SP-6 B-flow Phase 1f — Intraday Drift Atlas (PRE-REGISTERED, descriptive)

Date: 2026-06-07. Status: **PRE-REGISTERED — committed before any drift-curve
statistic has been computed.** Same sample-reuse caveat as Phase-1e: the
813-session cache is burned (kill-test grid, 1d, 1e observed); the per-minute
curve below has never been computed; rules lock here; any positive requires
forward confirmation before money moves.

## 0. Question + interpretation discipline

Operator question: does the COMMON intraday drift have a shape a fixed,
ticker-agnostic fill time can exploit (e.g., shorts at the open, longs
slightly after the open)? Phase-1e established the t=−30 rank IC is
predominantly common across-minute structure — this atlas MAPS that
structure unconditionally.

**Pre-stated interpretation rules:**
- A fixed-time fill harvests the UNCONDITIONAL mean drift fill→close. That is
  beta-window timing, not execution alpha, unless it deviates from the
  uniform-accrual null below. The atlas is DESCRIPTIVE; no outcome authorizes
  any live change. Its job is to inform the open-fill backtest variant
  (separate workstream) and quantify the operator's two named shapes.
- Cost caveat pre-stated: the half-range spread proxy INFLATES near the open
  (genuine volatility, partly real cost) and near the close (1e finding);
  gross and net are both reported, neither is treated as a tradable number.

## 1. Statistic (frozen)

Per session s, per decision minute m ∈ [0, 388]:
- Eligible tickers: pair-level eligibility (dump exists + registered
  60-valid-bar floor) AND finite G(m) (Phase-1d `delta_vectors` convention:
  fill = vw_{m+1}; G = LONG gross-to-dump bps; C = fill-bar spread − dump
  spread).
- curve_gross_s(m) = cross-ticker mean of G(m); curve_net_s(m) = mean of
  G(m) − C(m). Minute valid iff n_xs ≥ 50; else NaN.
- Session eligible iff ≥ 300 valid minutes. **≥ 700 eligible sessions or
  INVALID-DATA.**

Pooled: per-minute across-session mean + clustered t (session = cluster,
ddof=1), for gross and net. The LONG curve is reported; SHORT = −gross − C
(derivable; sign-mirrored gross, same cost).

**Uniform-accrual null + pre-named test points (multiplicity control):**
Under H0 (drift accrues uniformly), curve(m) = curve(0) · (389 − m)/389.
Deviation D(m) = curve_gross(m) − curve_gross(0)·(389 − m)/389, computed
per session then pooled (clustered t). Pre-named test minutes:
**m ∈ {5, 15, 30, 60, 120, 180, 240, 300, 330}**.

## 2. Pre-committed readouts

- **TIMING-STRUCTURE** ⟺ ≥ 2 ADJACENT pre-named points with |t(D)| ≥ 3 and
  the same sign. Else **FLAT**.
- Operator's named shapes, scored explicitly (descriptive):
  (i) "shorts at the open": curve_gross(0..5) significantly NEGATIVE
  (market opens above its close-ward path ⇒ short fills at open capture it);
  (ii) "longs slightly after the open": local minimum of curve_gross in
  m ∈ [10, 45] deeper than curve(5) with |t(D)| ≥ 3 there.
- Diagnostics: 7-bucket curves (sign stability), per-minute n_xs, cost curve
  C̄(m) (the venue-cost shape — relevant to the 1e +8.9t artifact and to any
  open-fill costing).
- Decision linkage: NOTHING goes live from this. TIMING-STRUCTURE feeds the
  open-fill backtest variant's design (which fill times to test); FLAT means
  fixed-time tweaks are not worth backtest-variant complexity beyond the
  plain open[t+1] case.

## 3. Computation

`src/research/bflow/drift_atlas.py` + `scripts/run_bflow_phase1f.py`,
reusing frozen surfaces only (`mr_policy.delta_vectors`,
`mr_policy._eligible_pair`, `run_phase1b` loaders, `mr_policy.BUCKETS`).
Single session-major pass, cache-only, sequential nice -19, run DETACHED
(systemd-run — session-exit kills hit 1e twice). Outputs
`analysis/bflow_phase1f/{report.md, curves.parquet}`; no-peek progress
lines; the report is the first look.
