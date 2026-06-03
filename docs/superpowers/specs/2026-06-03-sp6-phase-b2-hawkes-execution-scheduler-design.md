# SP-6 Phase B2 — Alpha-Conditioned Hawkes Execution Scheduler (Design)

- **Date:** 2026-06-03
- **Status:** Design — approved in brainstorming, pending spec review → implementation plan
- **Parent:** SP-6 Phase B. Builds on Phase A (`docs/superpowers/specs/2026-05-31-sp6-phase-a-eod-open-execution-design.md`) and B1 (`docs/superpowers/specs/2026-06-01-sp6-phase-b1-execution-scheduler-design.md` §8 deferred = this scope).
- **Depends on (live):** Phase A EOD→open lane live (tip `f3f366a`); B1 + B0 activated (the live executor reuses B1's planner/simulator/ledger and B0's per-order fill ledger). B2's *live* run rides the B1 go/no-go clock; B2's *build* and shadow-accrual do not.
- **Author:** BotJohn (brainstorming session 2026-06-03)

---

## 1. Objective

Replace Phase A's naive 3:55 PM into-close dump with an **alpha-conditioned 9:30→close participation curve** that works each open across the T+1 session to **beat `close[T+1]`** after a conservative haircut, and introduce a **low-dimensional self-exciting ("Hawkes") order-flow signal** that conditions the curve — gated so the signal can change *fills* only after it clears a falsifiable validation bar (**§28**: alpha-randomization + replay + OOS + operator sign-off).

We are on a **paper account**, so live testing carries no financial risk. The ONE thing paper does **not** relax: **`w_hawkes` defaults 0 and lifts off 0 only after §28 passes.** Paper removes financial risk, not the epistemic bar.

### What B2 is NOT
- Not a relaxation of the §28 gate. No fill is conditioned on the Hawkes alpha until §28 clears.
- Not a real-money path. Paper only; a hard runtime assert enforces it independent of gates.
- Not a change to the 9:28 OPG close reconcile (drops/flatten stay exactly as Phase A built them).
- Not a re-sizing of positions — B2 changes the **timing** of the same sized order, not its size.

---

## 2. Locked decisions (from brainstorming 2026-06-03)

1. **§28 = permutation core + operator sign-off (two-key):** alpha-randomization (time-misalignment null) + replay + OOS holdout as the falsifiable quantitative core, **then** an explicit operator go/no-go before `w_hawkes` lifts. Reconciles B1 §5 ("judgment, not a p-value") with the handoff's named tests and SP-6's operator-driven activation pattern.
2. **Week-1 scope = build AND run a live-PAPER `w_hawkes=0` executor** (after tonight's Phase-A fill verifies → B1/B0 live → operator approval). Honest justification: it exercises the real order path + validates against Alpaca's paper fill model + shakes out mechanics — it does **not** accelerate §28 (see §6/§9). It runs a curve whose own "beats close" thesis (B1 go/no-go) is still open; acceptable as paper data-gathering.
3. **Hawkes substrate = self-exciting intensity proxy on 30m features** (2–4 params, EWMA/decay kernels), **not** a fitted point-process. Decay sanity-checked, not fit (B1 §5 anti-overfit).
4. **Curve = capped tilt, causal, per-bucket re-plan.** `w_hawkes` tilts each slice within a hard cap so the Hawkes term reshapes but never dominates the VWAP base; the bound lives in the curve math.
5. **Two-bracket divergence = dual, each governs its own ledger.** Broker protective bracket anchored to the actual fill protects reality; the strategy ledger marks entry at `close[T+1]` and walks its re-anchored bracket for parity. The gap *is* the measured execution alpha.
6. **Safety = completion-floor only, no adaptive (outcome-conditioned) abort.** A price/P&L-conditioned abort would contaminate §28 (truncates the loss tail → survivorship inflation). Operational anomaly-halts (stale/NaN/broker-reject/spread-blowout → halt-and-sweep) stay — they are error-handling, not outcome-conditioned exits.
7. **Cutover = 9:30 size-once, B2 owns opens, 3:55 = backstop.** The production sizer runs once at ~9:30 (the sizing-timing cutover); B2 works the fixed sizes 9:30→close; legacy 3:55 sizer-fill is replaced by B2's terminal sweep; gate-selected single owner of opens.
8. **§28 null = time-misalignment** (circular-shift / block-shuffle of `ĥ` within session): preserves `ĥ`'s marginal distribution and autocorrelation, destroys its alignment to *when* moves happen. Tests the curve's actual mechanism (temporal alignment), survives B1 §5's skepticism.

---

## 3. Scope & internal build phasing

B2 is one spec with three build-phased layers. Gates are all default-OFF; **all-off ⇒ byte-identical to Phase A.**

| Layer | What | When built | When it runs | Gate |
|---|---|---|---|---|
| **B2.1** | Live `w_hawkes=0` executor (VWAP/U-shape base curve, real paper fills) + 9:30 sizing cutover + dual brackets + completion floor + simulator-calibration stream | this week (worktree, gated OFF) | live-PAPER, after B1/B0 live + operator approval | `OPENCLAW_B2_EXECUTOR` |
| **B2.2** | Hawkes substrate + capped-tilt curve + §28 shadow harness (Hawkes arm **simulated**; accrues the §28 `Δ` ledger) | this week (worktree) | shadow-only; **live** `w_hawkes=0` in every live path | `OPENCLAW_B2_HAWKES_SHADOW` + `OPENCLAW_B2_SHADOW_W_HAWKES` (candidate, nonzero) |
| **B2.3** | Lift `OPENCLAW_B2_W_HAWKES` off 0 in the live executor | deferred | only after §28 pass + operator sign-off | `OPENCLAW_B2_W_HAWKES` (live weight, default 0) |

**Two separate weight vars (load-bearing):** the shadow stream runs at `OPENCLAW_B2_SHADOW_W_HAWKES` (a **nonzero** pre-committed candidate — else `Δ = sim_hawkes − sim_base ≡ 0`, hollow accrual), while the live executor's `OPENCLAW_B2_W_HAWKES` stays **0** until §28 passes. They are deliberately distinct keys: one shared var would force a choice between a hollow shadow and a broken NON-NEGOTIABLE. The shadow driver warns loudly if its gate is on but the candidate weight is 0.

**Paper-only assert:** `b2_executor` refuses to submit unless the Alpaca account is paper — independent of every gate.

---

## 4. Architecture & reuse (do not rebuild)

**Reuse from B1** (`src/execution/`): `b1_planner.py` (base `slice(t) ∝ profile[t]·exp(−λ·sᵢ·t)`, U_SHAPE), `b1_simulator.py` (`vwap ± haircut`, NaN-vwap guards), `b1_order_source.py` (`es.id=sp.signal_id`, `signal_date`+T+1 reconstruction), `b1_ledger.py` / `b1_shadow_exec_ledger` (mig 128), `b1_run.py` (driver). `src/ingestion/ingest_prices_30m_alpaca.py` (append-only 30m ingester, NEVER-DELETE guard).

**Reuse from B0:** mig 127 `official_close` / `exec_ledger_usd` on `alpaca_submissions`; `finalize_execution_ledger` in `src/execution/parity_mark.py` (the per-order real-fill ledger B2.1 writes `actual_fill` into).

**Reuse from Phase A:** `open_reconcile.run_reconcile` (9:28 OPG closes — **untouched**), `regime_blended_sizer_live.py` (sizer, re-timed to 9:30 under the B2 gate), `parity_mark.finalize_parity_marks` (`close[T+1]` strategy mark + bracket re-anchor via `_reanchor_bracket`), `execute:close:inflight` Redis lock, `already_executed()`, `engine.update_pnl`.

**Reuse from 2026-05-29 OCO work:** `stop_reattach.py --oco` for the completion bracket.

**New modules** (all additive; gate-off inert):
| Module | Type | Responsibility |
|---|---|---|
| `src/execution/b2_hawkes.py` | pure fn | self-exciting continuation-intensity `ĥ(t)` from 30m features; **causal** (bars `< t` only) |
| `src/execution/b2_planner.py` | pure fn | capped-tilt wrapper over `b1_planner`; `w_hawkes=0` ⇒ byte-identical to base |
| `src/execution/b2_executor.py` | I/O | live child-order executor (9:30→close); paper-only; gated `OPENCLAW_B2_EXECUTOR` |
| `src/execution/b2_validation.py` | analysis | §28 harness: `Δ` ledger, time-misalignment null, replay, OOS, anti-peeking, min-n |
| migrations | schema | §28 results table; shadow `Δ` ledger (extend `b1_shadow_exec_ledger` or sibling) |

**Unit boundaries:** `b2_hawkes` and `b2_planner` are pure and independently testable; `b2_executor` is the only new live-I/O surface; `b2_validation` is offline analysis. The simulator/order-source/base-planner stay B1's.

---

## 5. The Hawkes substrate — `b2_hawkes.py` (B2.2)

A low-dim **self-exciting continuation-intensity proxy** on 30m bars (no point-process fit; no L2/trade tape available). Default 3 features via EWMA/decay kernels, evaluated on bars strictly before the current bucket:
- **Signed-return momentum clustering** — EWMA of 30m returns signed into the order's favorable direction.
- **Volume surprise** — `log(bucket_volume / trailing-N median)`.
- **Range-volatility clustering** — EWMA of `(high − low) / vwap`.

Features standardized → combined → squashed (`tanh`) → `ĥ(t) ∈ [−1, 1]`, signed so `+1` = strong continuation in the signal's favor. **Causality is enforced by test:** perturbing any bar `≥ t` cannot change `ĥ(t)`. Decay/half-life from a principled prior (~2 buckets ≈ 1 hour, tied to the intraday horizon), **sanity-checked against data, not fit.**

---

## 6. The capped-tilt curve — `b2_planner.py` (B2.2)

```
slice(t) ∝ profile[t] · exp(−λ·sᵢ·t) · (1 + clip(w_hawkes·ĥ(t), −c, +c))
```
- Monotone in `ĥ`; **no full pause** (clip floor `> −1`); re-normalized to the **remaining** qty each bucket (per-bucket adaptive re-plan).
- Cap `c` (proposed `0.5`) lives in the math, so the Hawkes term reshapes but never dominates the base.
- **`w_hawkes = 0` ⇒ the factor is 1 ⇒ collapses exactly to `b1_planner` (byte-identical).** Guarded by a regression test.
- **Refined look-ahead guard:** `plan(t)` may depend on bars `< t` (causal/adaptive), **never** on bars `≥ t`. (B1's static guard "any post-9:30 bar leaves the plan unchanged" is replaced by this causal form.)
- **Two known modeling questions (resolve empirically in §28 accrual, not by tuning):** (1) a *constant* `ĥ` across buckets renormalizes away entirely — the curve only reshapes on cross-bucket **variation** in `ĥ`. (2) Whether `ĥ`'s effect should **lead or lag** the "front-load to capture the move before it prices in" thesis is open: a causal momentum `ĥ` only turns positive *after* a move begins, which can back-load on a monotonic path. These are why a planted-signal test built on the real `ĥ` features may legitimately fail — that is signal about the feature form, not test flakiness. The §28 harness is tested via an injected `h_override` so the *machinery* is validated independent of the feature's lead/lag.

---

## 7. The live executor — `b2_executor.py` (B2.1)

**Cutover (the 3:55→9:30 sizing-timing cutover):**
1. 9:28 — `open_reconcile.run_reconcile` closes drops/flatten at the open (OPG dual-path), **unchanged**.
2. ~9:30 — run the production sizer **once** against 9:30 NAV/BP to fix target open sizes (sizes, not just timing, are pinned here). Re-fetch the broker book immediately before sizing (staleness guard); honor the existing DTBP guard.
3. 9:30→close — B2 works the fixed sizes on the capped-tilt curve. The executor calls `b2_planner` with `w_hawkes` from config (default 0, so it runs the base curve) — **B2.3 lifts the weight by config, not a code change.**
4. ~3:55 — B2's **terminal sweep** fills any residual into `close[T+1]` (replaces the legacy 3:55 sizer-fill of opens).

Gate `OPENCLAW_B2_EXECUTOR` selects the owner of opens: ON → B2; OFF → the legacy 3:55 sizer path, **byte-identical**. Only one path owns opens at a time → no double-execution.

**Child-order mechanics:**
- One **marketable-limit** child per 30m bucket (~13/session); crossing capped at `min(½·spread + k bps, hard cap)`.
- Unfilled child at bucket end → **cancel + roll** the remainder into the next bucket's re-plan.
- **Completion floor:** residual always sweeps into `close[T+1]` ⇒ **worst case ≡ Phase A**.
- **Operational anomaly-halts only:** stale bar / NaN / broker reject / spread blowout → halt-and-sweep. **No price/P&L-conditioned abort** (would contaminate §28).
- **Idempotency:** deterministic per-child coid (`date+ticker+bucket`) through `already_executed()`; crash/replay never double-submits.
- **Paper-only assert** before any submit.

**Dual brackets (§5 locked):** the broker protective OCO is placed **at completion**, anchored to the actual avg fill (via `stop_reattach.py --oco`); the strategy ledger marks entry at `close[T+1]` and walks its `_reanchor_bracket` bracket for parity. **Accepted corollary:** the position carries **no broker bracket during 9:30→close accumulation** — consistent with the backtest's daily-close exit model (strategy-side stops can't fire intraday anyway), so parity holds. Exit-timing divergence between the broker bracket and the strategy ledger is a documented live-only divergence (extends Phase A §3.1).

**Concurrency (surface ≫ Phase A — orders work for hours):** extend the `execute:close:inflight:{date}` lock to cover the whole 9:30→close window. A confirmed `*/5` intraday regime redeploy mid-schedule **cancels working children + re-plans** (the redeploy already nets deltas), guarded by the lock — *not* a footnote; a tested path.

---

## 8. Two validation streams + simulator calibration

Two parallel, complementary data streams (different validation bars):
- **Stream A — live real paper fills (B2.1):** the `w_hawkes=0` executor's real fills land in `alpaca_submissions` via B0's per-order ledger. Validates B1's "working beats close" thesis with paper fills and exercises the real order path.
- **Stream B — shadow simulation (B2.2):** both curves simulated on identical realized bars → `Δ = ledger_hawkes − ledger_base` per order, where the Hawkes arm runs at the **shadow candidate weight `OPENCLAW_B2_SHADOW_W_HAWKES`** (nonzero — distinct from the live `OPENCLAW_B2_W_HAWKES=0`). **`Δ` is forced sim-vs-sim:** the NON-NEGOTIABLE forbids running the Hawkes curve on any live order before §28, so the Hawkes arm *must* be simulated. **The live `w_hawkes=0` stream's only role in §28 is calibrating the simulator** — implementers must compute `Δ = sim_hawkes − sim_base`, never `real_base − sim_hawkes`.

**Simulator calibration:** compare `real_base` (Stream A) vs `sim_base` (`b1_simulator` on the same orders) → haircut-realism report.

---

## 9. §28 harness — `b2_validation.py` (the B2.3 gate)

- **Statistic:** `Σ Δ` over accrued orders, `Δ = ledger_hawkes − ledger_base` per order (same order, same realized bars; only the `w_hawkes`-tilted allocation differs — isolates the Hawkes term's contribution).
- **Null (time-misalignment):** re-run the Hawkes curve with `ĥ(t)` circularly-shifted / block-shuffled within each session, preserving `ĥ`'s marginal distribution and autocorrelation while destroying its alignment to the realized price path. **N ≥ 1000** scrambles → null distribution of `Σ Δ`.
- **Pass bar (all four):** real `Σ Δ` > **95th percentile** of the null **AND** positive on a **time-ordered OOS holdout** **AND** the Hawkes curve beats the `w_hawkes=0` base **AND** explicit **operator sign-off** (two-key).
- **Anti-peeking, time axis (protects the NON-NEGOTIABLE):** re-evaluating §28 every week as data accrues inflates the false-positive rate far past 5%. So the gate adds a **min-n precondition** (§28 is not even *evaluated* before n is reached) **and** a **multiple-looks discipline** — pre-committed evaluation point and/or pass-must-hold-across-consecutive-looks (alpha-spend). **`w_hawkes` stays 0 for weeks**; accrual grows n, it does not shorten the gate.
- **Anti-peeking, parameter axis:** the time-misalignment null does **not** account for a search over `(w_hawkes, cap)`. Trying several until one clears the 95th-pct bar is the same multiple-comparisons inflation. So the candidate `(w_hawkes, cap)` is **fixed before the evaluation window opens** (and equals what the shadow stream actually accrued at). If a search is unavoidable, the search itself must run *inside* the null (re-select on each scramble), not outside it.

---

## 10. Error handling & failure modes

| Failure mode | Guard |
|---|---|
| Missing / stale 30m bars for a bucket | skip + count; never impute (imputation fabricates alpha) |
| NaN vwap / non-finite feature | B1's `not(>0)` NaN guard at every layer; feature NaN → `ĥ` contribution 0 |
| Broker reject / spread blowout mid-schedule | operational halt-and-sweep into close (not an outcome-conditioned abort) |
| Crash / re-run mid-schedule | deterministic per-child coid + `already_executed()` idempotent skip |
| `*/5` regime redeploy collides with working children | `execute:close:inflight` lock; redeploy cancels + re-plans |
| Live account misconfigured | hard paper-only assert refuses to submit |
| §28 peeked early on a lucky window | min-n precondition + pre-committed eval / consecutive-look discipline + operator sign-off |
| Implementer wires `Δ = real_base − sim_hawkes` | spec + test fix `Δ = sim_hawkes − sim_base`, both sim, same bars |

---

## 11. Gates, rollout & sequencing

**Gates (default-OFF):** `OPENCLAW_B2_EXECUTOR`, `OPENCLAW_B2_HAWKES_SHADOW`, `OPENCLAW_B2_SHADOW_W_HAWKES` (shadow candidate, nonzero), `OPENCLAW_B2_W_HAWKES` (live, default 0). **All-off ⇒ Phase A byte-identical** (regression-proven).

**Rollout (reversible, gated):**
1. **Build** B2.1 + B2.2 this week in a worktree (`feat/sp6-phase-b2-execution-scheduler`) **off the merged SP-6 base (after B0+B1 merge — NOT bare `f3f366a`, which has no B1 code to reuse)**, subagent-TDD, gates OFF. Nothing deployed.
2. **Prereq:** tonight's Phase-A into-close fill verifies → B0 + B1 activated per the existing runbook (separate operator action; B2 does not perform it).
3. **Activate B2.1 (live-PAPER, operator-present):** flip `OPENCLAW_B2_EXECUTOR` ON with `OPENCLAW_B2_W_HAWKES=0`; wire the 9:30 size-once + 9:30→close executor crons; restart johnbot. Legacy 3:55 path disabled by the gate (single owner).
4. **Activate B2.2 (shadow):** flip `OPENCLAW_B2_HAWKES_SHADOW` ON **AND set `OPENCLAW_B2_SHADOW_W_HAWKES` to the pre-committed candidate (e.g. `1.0`)** → §28 `Δ` accrues daily on live opens (nonzero). No live effect. (At candidate 0 the driver warns: Δ≡0.)
5. **Calibrate:** monitor `real_base` vs `sim_base`; confirm the order path + paper fills behave; watch the dual-bracket completion placement.
6. **§28 (weeks out):** at the pre-committed min-n/date with the pre-committed `(w_hawkes, cap)`, run `b2_validation`; if the four-part bar clears across the committed looks + operator signs off → **B2.3: set `OPENCLAW_B2_W_HAWKES` to the validated weight.** Otherwise B2 stops at the live `w_hawkes=0` executor — a clean, successful negative result.

**Standing constraints:** paper only; NEVER delete master data (incl. `prices_30m.parquet`); 2-core/8GB (nice -n 19, no concurrent heavy jobs); no push / no johnbot restart / no deploy without operator approval; don't disturb the live checkout's uncommitted `manifest.json` / `strategy_signatures.json` / `run_sentiment_step.py`; ABORT is never `git reset --hard`.

---

## 12. Test plan (TDD)

- **`b2_hawkes`:** causality (any bar `≥ t` cannot change `ĥ(t)`); feature math on fixtures; NaN → 0 contribution; `ĥ ∈ [−1,1]` and direction sign.
- **`b2_planner`:** `w_hawkes=0` ⇒ byte-identical to `b1_planner`; cap clipping at `±c`; monotonicity in `ĥ`; slices sum to remaining qty; refined look-ahead.
- **`b2_executor`:** gate-on → B2 owns opens, gate-off → legacy 3:55 byte-identical; completion floor (residual → close); anomaly-halt → sweep; idempotent coids (replay no double-submit); OCO placed at completion anchored to avg fill; redeploy cancels children; paper-only assert blocks a live-account submit.
- **`b2_validation`:** null preserves marginal + autocorrelation; `Δ` sign and `sim_hawkes − sim_base` orientation; min-n gate blocks early evaluation; time-ordered OOS split; **planted-signal fixture passes, pure-noise fixture fails** (harness sanity).
- **Parity regression:** all `OPENCLAW_B2_*` OFF + `w_hawkes=0` ⇒ Phase A / B1 / B0 suites unchanged.

---

## 13. Documented limitations (not bugs)

- **Paper fills are themselves simulated** (Alpaca's fill model, not real microstructure). The live executor catches gross mechanics errors + exercises the order path; it does **not** ground-truth slippage. The §28 `Δ` is sim-vs-sim regardless, so the live executor does **not** accelerate §28.
- **`real_base` vs `sim_base` calibration is only meaningful if** the simulator's fill model is aligned to the executor's marketable-limit-per-bucket mechanics — otherwise it measures a mechanics mismatch, not haircut error. Align the simulator before trusting the calibration.
- **Cancel/replace + partial-fill roll likely won't be exercised on paper** (Alpaca paper fills marketable limits whole/immediately) → keep those unit tests honest; the live run won't validate that path.
- **No broker bracket during 9:30→close accumulation** (accepted corollary of completion-floor + bracket-at-completion).
- **DST:** `bucket_of` is EDT-aware; refine before winter dates (inherited B1 follow-up).

---

## 14. Open questions / operator-tunable defaults (proposed, flagged in the plan)

- Cap `c = 0.5`; `ĥ` features = {signed-return momentum, volume surprise, range-vol clustering} (3); EWMA half-life ≈ 2 buckets.
- **Shadow candidate `OPENCLAW_B2_SHADOW_W_HAWKES`** (proposed `1.0`) — the nonzero weight the §28 `Δ` is measured at. **Must be pre-committed (with `cap`) before the evaluation window opens** (parameter-axis anti-peeking, §9); the live `OPENCLAW_B2_W_HAWKES` stays 0.
- §28: `min-n` (proposed ≥ 150–200 distinct order-day observations), first-eval date pre-committed (~4 weeks post-activation), pass must hold across 2 consecutive monthly looks, OOS split time-ordered 70/30.
- Child marketable-limit crossing cap `k bps` + hard cap.
- These are written into the spec as concrete defaults and surfaced in the implementation plan as operator-tunable knobs.
