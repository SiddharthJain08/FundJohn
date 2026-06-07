# SP-6 B-flow Phase 1e — Cross-Sectional Discriminator (PRE-REGISTERED)

Date: 2026-06-07. Status: **PRE-REGISTERED — committed before the L/S spread
statistic has ever been computed on any data.**

## 0. What this discriminates, and the sample-reuse caveat (stated plainly)

The kill-test found overwhelming rank-IC predictability (pooled t −23..−31);
Phase-1d found ZERO per-name entry-timing harvest. Two live explanations:
- **(a)** the edge is CROSS-SECTIONAL (which names bounce relative to others)
  — harvestable in portfolio/selection form only;
- **(b)** the IC is common time-of-day / market-drift structure that rank-ICs
  pool in — no per-name execution content; Phase-2 Hawkes pre-doomed.

A dollar-neutral L/S decile spread formed within each minute differences out
ALL common structure by construction — (b) predicts spread ≈ 0, (a) predicts
spread > 0.

**Sample reuse acknowledged:** this runs on the same 813-session historical
cache whose IC grid and 1d policy results have been observed. The (a)/(b)
hypotheses were formed after those observations. Mitigation: the spread
statistic below has never been computed anywhere; all rules lock in this
commit; ANY positive outcome requires forward confirmation before any live
use. This is a diagnostic, not a confirmatory test.

## 1. The statistic (all constants frozen here)

Per session s, per decision minute t ∈ [30, 383]:

- Cross-section = tickers eligible at pair level (dump exists + registered
  60-valid-bar floor) AND finite `vwap_disp_30(t)` AND finite G(t) (valid
  fill bar t+1, exactly Phase-1d's `delta_vectors` convention: fill =
  vw_{t+1}, G = LONG gross-to-dump bps, C = fill-bar spread − dump spread).
- **Minute eligible iff n_xs ≥ 50**; else skipped.
- k = floor(0.10 · n_xs). LONG leg = k smallest by vwap_disp_30 (deepest
  below VWAP); SHORT leg = k largest. Ties broken by ticker lexicographic
  (deterministic). Equal weight within leg.
- **spread_net(t)** = mean_long(G−C) + mean_short(−G−C)
- **spread_gross(t)** = mean_long(G) − mean_short(G)

Session value = mean over eligible minutes; **session eligible iff ≥ 100
eligible minutes**. Statistic: across-session mean and clustered
t = mean/(sd/√n_sessions), session = cluster (registered Test-A shape).
**≥ 700 eligible sessions required for any verdict; else INVALID-DATA.**

Economic reading: the average net result of a one-shot dollar-neutral L/S
(enter both legs at a uniformly random minute, exit both at the dump) —
the convertibility object matching our one-entry-per-day execution context.
NOT a claim about a 354-rebalance/day strategy.

## 2. VERDICT RULES (pre-committed, zero free parameters)

- **ECON-PASS**: net t ≥ +3 — cross-sectional content exists AND survives
  the differential spread cost. Hypothesis (a), economically alive.
- **SIGNAL-ONLY**: gross t ≥ +3 AND net t < +3 — (a) confirmed but
  cost-eaten at this entry/exit form.
- **NULL**: gross t < +3 (and > −3) — no cross-sectional content;
  hypothesis (b); the flow channel is economically empty per-name AND
  cross-sectionally; Phase-2 tick work is NOT justified by this signal.
- **INVERTED**: gross t ≤ −3 — reported first-class (cross-sectional
  momentum), same forward-confirmation requirement.

Diagnostics (non-gating): per-bucket (the 7 calendar buckets) means + signs;
per-leg decomposition vs cross-sectional minute mean; mean n_xs; k
distribution; gross vs net gap (cost drag).

Decision linkage (asymmetric, as always): NO outcome authorizes anything
live. ECON-PASS ⇒ justifies designing a forward-confirmable portfolio/
selection use of the signal + keeps Phase-2 candidacy alive. SIGNAL-ONLY ⇒
signal exists; any use must find a cheaper harvesting form. NULL ⇒ close the
cross-sectional question; July gate remains predictability-only.

## 3. Computation

`src/research/bflow/xsec_discriminator.py` + `scripts/run_bflow_phase1e.py`,
REUSING frozen surfaces only: `mr_policy.delta_vectors` (G/C),
`flow_features.compute_features` (vwap_disp_30), `mr_policy._eligible_pair`,
`run_phase1b.{enumerate_cache_sessions, load_session_frame, _ticker_frames}`.
Single session-major pass; cache-only; sequential nice -19. Outputs
`analysis/bflow_phase1e/{report.md, spread_sessions.parquet}` + a
`[bflow-p1e] VERDICT:` line. The runner's verdict block is the first look at
any spread number.
