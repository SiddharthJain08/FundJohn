# SP-6 B-flow Phase 1d — MA-Reversion Entry Policy (PRE-REGISTERED design)

Date: 2026-06-06. Status: **DESIGN APPROVED by operator + PRE-REGISTERED — this
commit is the lock, made strictly BEFORE any historical IC or policy statistic
has been observed** (the 2023..2026-03 historical pull is still in flight at
lock time; its log contains row counts only, per the kill-test prereg §6
no-peek rule, which this document extends to itself).

## 0. Relationship to the other registered tests — ASYMMETRIC

Same discipline as the historical kill-test
(`2026-06-06-sp6-bflow-phase1b-historical-killtest-prereg.md`):

- A historical PASS **cannot authorize live deployment**. It authorizes ONE
  thing: building the forward shadow lane (proposed entries shadowed against
  the actual 3:55 dump fills on live sessions, n ≥ 20 forward sessions before
  any cutover conversation).
- A historical FAIL closes the idea at minute scale. Recorded, not re-tuned
  on this sample.
- This policy is the *policy form* of the registered f3 feature
  (`vwap_disp_30`): "price below trailing 30-min VWAP → enter long" trades
  exactly the object whose IC the kill-test measures. Pre-committed
  interpretation hierarchy: if the kill-test verdict is KILL, a pass here is
  near-impossible and would be treated with maximal suspicion; the policy run
  executes REGARDLESS of the kill-test verdict (mechanical, no discretionary
  fork), in that order (kill-test evaluator first).

## 1. The policy (causal; all constants frozen here)

LONG leg, per (ticker, session):

- Scan minutes t ∈ [30, 383]. At end of minute t compute
  `flow_features.compute_features` → `vwap_disp_30(t)` and its
  within-session trailing z via `energy_counterfactual.running_z` applied to
  the session's vwap_disp_30 series — conventions VERBATIM from the M3
  machinery: t-INCLUSIVE trailing window, sample sd (ddof=1), z is NaN until
  ≥ 2 finite trailing values and NaN where trailing sd = 0; NaN never crosses
  a threshold.
- **Trigger** = first t with z(t) ≤ −ζ AND a valid fill bar at t+1
  (`valid_bar`: vw>0, v>0, h≥l, NaN-guard `not (x > 0)`). If bar t+1 is
  invalid the trigger is VOID and scanning continues.
- **Fill = vw_{t+1}** (decision at end of minute t, fill in the next minute —
  the Test-B causal convention).
- **Forced fallback**: if never triggered by t = 383, fill = the dump
  benchmark `oracle.dump_benchmark` (minute ≥ 385 vol-weighted vw). Fallback
  entries score Δ = 0 and matched-null = 0 by construction (entering at the
  benchmark IS the benchmark); they stay IN the per-session means —
  unconditional accounting, no selection on triggered days.
- One entry per (ticker, session, leg, ζ).

SHORT leg mirrors (trigger z(t) ≥ +ζ; leg-signed economics).

ζ grid (pre-enumerated): **ζ ∈ {1.0, 1.5, 2.0}** → 3 cells × 2 legs = 6
primary cells. Cells within a leg are nested (ζ=1.0 triggers ⊇ ζ=2.0) and
therefore correlated; the leg-level pass rule below accounts for this.

## 2. Book + eligibility

- Synthetic all-pairs both-legs book: every (ticker, session) in the frozen
  505-ticker universe (`analysis/bflow_phase1b_hist/universe_505.txt`) ×
  eligible historical sessions. No real intents exist pre-2026; both legs are
  always evaluated and **scored separately**.
- Pair eligibility: session in [2023-01-03, 2026-03-31]; dump benchmark
  exists (early closes self-exclude); ticker passes the registered
  60-valid-bar floor that session.
- Data: the kill-test's historical cache `data/cache/min_bars_hist/`,
  read via `minbar_cache.get_session_bars` with a poisoned fetcher
  (cache-only; this test NEVER fetches).

## 3. Economics, null, and PASS BAR (pre-committed)

Per triggered entry:

- **Δ_net bps vs dump** = leg-signed gross (dump vs fill) minus the frozen
  differential spread cost: `oracle.spread_bps`(entry-minute bar) −
  `oracle.eod_dump_window_spread_bps`(session bars), exactly the
  `flow_policy._delta_bps` construction (cost model min(0.5·(h−l)/vw·1e4,
  50)+2bps parity form lives in oracle and is reused, not re-derived).
- **Minute-matched null** (the drift-proofing core): for entry at minute m,
  ticker k, session s — null = the LEAVE-ONE-SESSION-OUT mean over eligible
  sessions s′ ≠ s of the SAME net quantity for an unconditional entry at the
  same minute: leg-signed net Δ of fill vw_{k,s′,m+1} vs that session's dump.
  Computed per (k, m) over all eligible sessions and LOSO-adjusted
  ((Σ − x_s)/(n−1)). **Null validity floor (pre-committed): a (k, m) null
  needs ≥ 30 LOSO observations; an entry whose null has fewer is excluded
  from scoring and counted in the data-quality table.** This subtracts the
  deterministic minute→close drift curve per ticker, per leg — the exact
  channel that manufactured Test-B's +22bps mirage.
- **Per-entry excess e = Δ_net − null**; fallback entries e = 0.

Statistic: per-session mean of e across the session's pairs → across-session
mean and clustered t = mean/(sd/√n_sessions) — the registered Test-A
statistic shape, session = the cluster.

**PASS rules (zero free parameters at eval time):**

- A cell (leg, ζ) passes ⟺ excess t ≥ +3.
- **A leg passes ⟺ ≥ 2 of its 3 ζ cells pass** (no lone max-t survivor).
- **Downside guardrail (RELATIVE, pre-committed):** for a passing leg, the
  p95 adverse Δ_net-vs-dump of TRIGGERED entries must not exceed by more
  than **10bps** the p95 adverse of the minute-matched unconditional
  distribution — the pool over the leg's triggered entries (k, s, m) of
  {Δ_net(k, s′, m) : s′ ≠ s eligible} (same tickers, same minutes,
  unconditional sessions). (Any earlier
  entry carries variance vs the close; the deployable question is whether
  the dip-trigger makes the tail WORSE than generic same-minute entry.) A
  leg that passes the t-bar but breaches the guardrail is reported
  PASS-WITH-TAIL-BREACH and does NOT authorize the shadow lane.
- Verdict vocabulary: per leg ∈ {PASS, PASS-WITH-TAIL-BREACH, FAIL}.

Diagnostics (reported, never gating): strict two-leg-vs-dump (M3 bar:
both legs' raw Δ_net positive), trigger rate, fallback rate, entry-minute
histogram, per-bucket sign consistency on the kill-test's 7 calendar
buckets, absolute tail tables P(Δ_net < −tol) for tol ∈ {5, 10, 25}bps,
hit-rate P(fill no worse than dump + tol).

## 4. Sequencing + no-peek

- This spec + the implementation (module, runner, tests) are committed BEFORE
  the kill-test evaluator prints any historical IC. Run order on pull
  completion: (1) kill-test harness + evaluator, (2) Phase-1d runner. Both
  consumers of the fresh cache were locked before the first look at it.
- The Phase-1d runner prints its own verdict block; rules above have no free
  parameters. No threshold, window, or sign may be adjusted after first run.

## 5. Module layout

`src/research/bflow/mr_policy.py` (pure: trigger scan, fills, Δ_net, LOSO
null, excess; mirrors `flow_policy.py` patterns and REUSES `oracle.*` cost
trio + `running_z` + `compute_features` — no re-derived math),
`scripts/run_bflow_phase1d.py` (runner: enumerate hist cache sessions →
per-session policy rows → LOSO null pass → grids + verdict →
`analysis/bflow_phase1d/{report.md, policy_rows.parquet}`). TDD; suite runs
sequential `nice -n 19` (2-core box). Tests cover: trigger/void/fallback
paths, z-convention parity against `running_z`, fallback e=0 identity, LOSO
null arithmetic, leg sign mirroring, guardrail computation, and a frozen
synthetic-session fixture end-to-end.
