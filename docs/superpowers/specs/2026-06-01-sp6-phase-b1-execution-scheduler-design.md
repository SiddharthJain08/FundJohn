# SP-6 Phase B1 — Intraday Execution Scheduler, Shadow/Measurement Build (Design)

**Date:** 2026-06-01
**Status:** Design — observation-only first build; live executor deferred behind a go/no-go.
**Parent:** SP-6 Phase B (`docs/superpowers/specs/2026-05-31-sp6-phase-a-eod-open-execution-design.md` §10 out-of-scope, §13 prerequisite).
**Depends on (live):** Phase A `close[T+1]` parity mark (live, paper, 2026-06-01). **Not** on B0 (the shadow ledger is self-contained; B0 is only needed for the later live executor).

---

## 1. Objective

Phase A fills new/resize **opens** with a naive single-point dump at 3:55 PM into `close[T+1]`. B1 asks whether **working that same order along 9:30→close** beats the close — i.e. captures **beat-close execution alpha** as a distinct, measurable stream on top of strategy P&L (the strategy ledger stays marked at `close[T+1]`, so the execution ledger `(close[T+1] − actual_fill)·signed_qty` is pure execution alpha).

**Win condition (locked):** maximize execution-ledger $ **with a guardrail** — never worse than the naive-3:55 fill by more than a tolerance, and maintain fill completion by close.

**This first build is observation-only.** It runs *alongside* untouched Phase A and answers one question — *does the edge exist, after costs?* — as a **go/no-go** gate for building a live executor. If the edge is tiny or cost-dominated, B1 stops here: a cheap, successful negative result.

### What this build is NOT
- No live child orders, no order submission, no touching the 3:55 fill.
- No sizing-timing change (moving open-sizing 3:55→9:30 is the *live cutover*, a later sub-project).
- No Hawkes / order-flow term (B2, §28-gated). No impact/Almgren term. No close-side scheduling (closes stay at the 9:28 OPG reconcile).

---

## 2. Mathematical basis (the primary justification)

We trust the mathematics first; backtesting is targeted confirmation, not the proof (§5).

Let a signal decided at `close[T]` predict a positive `T+1` return for a long (symmetric for shorts). If the signal has genuine predictive power, the favorable price move **realizes during the T+1 session**. Therefore:

- **The close is the worst entry of the day.** By the time price reaches `close[T+1]`, the predicted move has (in expectation) already happened. A naive 3:55 dump pays the fully-moved price. Any earlier worked fill along 9:30→close enters *before* part of that move.
- **Beat-close alpha = intraday realization of the signal's own edge.** `E[close[T+1] − fill]` for a worked order equals the portion of the signal's predicted move that occurs after the average fill time. This is positive exactly to the degree the signal is real and its move is intraday-paced.
- **Front-loading by conviction follows directly.** If edge decays as it prices in, weight `w(t) ∝ base_vol(t)·exp(−λ·sᵢ·t)` shifts mass earlier for high-conviction `sᵢ`, capturing more of the move. `λ=0` ⇒ pure VWAP base (no view, only variance/adverse-selection reduction).
- **Even with zero directional edge**, working the order reduces timing variance and avoids close-auction adverse selection vs a single point — a weaker but real benefit (the VWAP-base case).

**The honest open question** this build answers: these are *daily-horizon* signals; whether their edge has **intraday-exploitable structure on T+1** (vs. moving overnight/at the open and being flat through the day) is empirical. The math says *if* there is intraday drift in the signal's favor, B1 captures it; the case studies (§5) test whether there is.

---

## 3. Architecture & components (all new, all additive)

| # | Component | Type | Responsibility |
|---|---|---|---|
| 1 | `intraday_bars` refresh | ingestion | Chunked backfill + daily append of 30-min bars (Alpaca `data bars --timeframe 30Min`) — **only for the curated case-study tickers/dates + the live traded universe going forward**, not the whole universe-history. Chunked + watchdog'd (2-core/8GB; weekend-OOM lesson). Writes `data/master/prices_30m.parquet` (append-only; currently stale/sparse). |
| 2 | participation-curve **planner** | pure fn | `plan(order{ticker, signed_qty, sᵢ}, expected_vol_profile, λ) → [(bucket, slice_qty)]` over 9:30→close. `slice ∝ base_vol(t)·exp(−λ·sᵢ·t)`, normalized to `signed_qty`. **Consumes only ≤9:30 info** (expected/trailing volume profile, never today's realized volume). No I/O. |
| 3 | shadow **simulator** | pure | `simulate(plan, realized_bars, close_T1) → {actual_fill, exec_ledger, naive_ledger, vwap_base_ledger, completion}`. Fills each slice at `bar_vwap ± (½·spread + impact_bps)` (conservative haircut so frictionless fills can't fake the edge). `exec_ledger = (close_T1 − actual_fill)·signed_qty`. Also computes the naive-3:55 baseline (= the *actual* Phase-A fill when available, else `close_T1`) and the VWAP-base baseline (`λ=0`). |
| 4 | order source | adapter | Reads the order to "work": (a) **live shadow** — today's actual opens from `alpaca_submissions`; (b) **case-study replay** — selected historical opens from `signal_pnl`. No re-sizing; B1 measures *timing of the same qty*. |
| 5 | shadow **ledger + report** | persistence | Own table `b1_shadow_exec_ledger` (NOT `alpaca_submissions`). Periodic report: beat-close bps + $, completion %, vs **both** baselines, **per regime** → posts to Discord #data-alerts (webhook UA fix landed 2026-06-01). |

**Unit boundaries:** planner and simulator are pure and independently testable; ingestion and persistence are the only I/O; the order-source adapter isolates "where the order comes from" so case-study and live shadow share one path.

**Gate:** `OPENCLAW_B1_SHADOW` (default OFF). Observational only — no order path, no live-cycle change; a bug cannot reach live trading.

---

## 4. Data flow

**One-time (curated):** pick case-study positions from `signal_pnl` (§5) → chunked backfill 30-min bars for just those ticker/date ranges.

**Case-study replay (the targeted validation):** per curated order → planner (expected profile + sᵢ-decay, causal) → simulator (realized bars + haircut) → ledger vs naive-close & VWAP-base → aggregate by case + per regime.

**Daily live shadow (ongoing, additive):** after the 4:15 PM parity mark (so `close[T+1]` is known), replay over today's actual Phase-A opens → append `b1_shadow_exec_ledger` → periodic Discord report. This is the most robust evidence over time: real positions, real bars, accumulating.

---

## 5. Validation philosophy (per operator steer, 2026-06-01)

Robust statistical OOS backtesting of intraday — and certainly minute-wise — execution is **not feasible** at the fidelity that would make a p-value honest. So:

1. **Mathematics is primary** (§2). The thesis stands or falls on whether the signals have intraday-paced edge; the math says how B1 captures it if they do.
2. **Minimal, feasible, targeted backtesting** — curate real prior positions where execution *mattered*: heavy losers a better entry would have softened, and big movers where a better entry would have captured materially more (rank `signal_pnl` by realized loss magnitude and by favorable intraday range). Replay B1 on just those at 30-min granularity; show concrete $ impact per case.
3. **λ from a principled prior** tied to the signal's decay/holding horizon, *validated* (sanity-checked, not heavily fit) on the case studies — there isn't enough robust data to fit aggressively, and over-fitting λ would be the exact trap the operator is flagging.
4. **Live shadow accrues the real evidence** going forward (real fills-vs-close on real opens).

**Go/no-go (judgment, not p-value):** proceed to a live executor only if (a) the math holds, (b) the curated case studies show material, after-cost beat-close on the positions that matter (beating *both* the naive-3:55 and the VWAP base), and (c) the early live-shadow ledger trends consistently. Otherwise B1 stops here.

---

## 6. Error handling & failure modes

- **Missing bars for a ticker-day → skip the order** (log + count); never impute (imputation fabricates alpha). Coverage % is reported and gates which cases are admissible.
- **Look-ahead guard:** the planner consumes only ≤9:30 info (expected/trailing volume profile, not today's realized volume); enforced by a test (§7) that feeding future bars cannot change the plan.
- **Stale/partial intraday data:** `prices_30m.parquet` is currently sparse; the refresh (component 1) is a prerequisite for any case to be admissible.
- **Containment:** gated OFF by default; writes only `b1_shadow_exec_ledger`; no code path submits or cancels an order. Safe to run on the just-activated live Phase A.

---

## 7. Test plan (TDD)

- **Planner:** slices sum to `signed_qty`; `λ=0` ⇒ pure VWAP-base weights; higher `sᵢ` ⇒ strictly more front-loaded mass; rejects/ignores realized-volume input (causality).
- **Simulator:** fill = `vwap − haircut` for buys / `vwap + haircut` for sells; ledger sign correct (buy filled below close ⇒ positive); both baselines computed; a fixed fixture day ⇒ exact expected ledger.
- **Coverage/skip:** missing-bar order ⇒ skipped and counted, not imputed.
- **Look-ahead test:** perturbing post-9:30 realized bars leaves the plan unchanged.
- **Integration smoke:** replay a handful of real curated case-study positions ⇒ sane ledger + report; report correctly classifies beat / tie / loss vs both baselines.

---

## 8. Gates, dependencies, deferred

- **Gate:** `OPENCLAW_B1_SHADOW` (default OFF).
- **Dependencies:** intraday-bars refresh (component 1); Phase A `close[T+1]` mark (live). **Not** B0.
- **Sequencing:** B1 *build* waits for Tuesday's Phase-A live-green (per the SP-6 memory gate); the design lands now. Tonight's verify-watchdog is the live priority.
- **Deferred (each contingent on go/no-go):** live child-order executor + B0 merge (real `actual_fill`); the 3:55→9:30 sizing-timing cutover; B2 Hawkes term + §28 weight-gate; impact/Almgren term; close-side scheduling; the two-bracket-set divergence.

---

## 9. Open questions

- **Expected volume profile source:** trailing-N-day per-ticker shape vs. a market-wide U-shape prior — start with trailing-N (fallback U-shape when sparse); revisit if coverage is thin.
- **Haircut calibration:** spread proxy (bar high-low fraction) + fixed impact bps by liquidity tier — pick conservative defaults; the go/no-go must survive them.
- **Case-study selection size (default):** top ~25 closed positions by realized-loss magnitude + top ~25 by favorable intraday range, from `signal_pnl` (de-duplicated) — adjust at curation if coverage is thin. `sᵢ` (per-order conviction, normalized to `[0,1]`) is taken from the signal record's `position_size_pct` (case-study) / `pct_nav` (live), divided by the 0.25 daily-cap; orders lacking it fall back to `sᵢ=0`, which makes `exp(−λ·sᵢ·t)=1` → pure VWAP base (no front-loading) for that order.
