# SP-6 — Hybrid Morning-Window Execution Feasibility (PRE-REGISTERED)

Date: 2026-06-08. Status: **PRE-REGISTERED — model / windows / verdict locked
before any number is computed.** Operator proposal (refined to a HYBRID):
- **SELL = PASSIVE limit** (earn the wide morning spread), patient: a morning
  window AND an afternoon retry window before a forced MOC. Sells can wait.
- **BUY = MARKETABLE** within a single morning window (cross the spread; the
  only lever is *timing within the window*), forced market at the deadline.

Question: does this hybrid have any possibility of beating the current close
baseline — before we design timing algorithms? The two sides have opposite
spread economics (sells EARN it, buys PAY it), so they're tested separately
and combined.

## 0. Structure under test

- **SELL orders** (long close/reduce + short open/increase): passive sell limit
  in **9:35–9:40 ET** (minutes 5–10); if unfilled, retry passive in
  **15:30–16:00 ET** (minutes 360–389); if still unfilled, forced MOC at 16:00.
- **BUY orders** (long open/increase + short close/reduce): MARKETABLE buy timed
  within **9:45–10:00 ET** (minutes 15–30); forced market at 10:00 (minute 30).
- Baseline = current Phase A: everything marketable at the ~16:00 close.

## 1. Data & model

- Source: `data/cache/min_bars_hist/` (813 sessions 2023–2026, frozen 505-name
  universe `analysis/bflow_phase1b_hist/universe_505.txt`). Minute-indexed
  (0 = 09:30); o/h/l/c/v/vw. Mid proxy = `vw` per minute.
- Close benchmark = `oracle.dump_benchmark` (vol-weighted vw, minute ≥ 385);
  sessions with no dump (early close) dropped.
- Half-spread `hs` (bps) is a PARAMETER (bars carry no bid/ask): run at
  **hs ∈ {2, 5, 8} bps** (spread study's measured morning range). Close
  half-spread `hs_c = 1.0 bps` (assumptions, not fitted).

### SELL side — PASSIVE (earn the spread)
- mid₅ = vw at minute 5; `ask_m = mid₅·(1 + hs/1e4)`. Morning FILL iff
  `max(h, minutes 5..10) ≥ ask_m` → fill at `ask_m`.
- If unfilled: mid₃₆₀ = vw at minute 360; `ask_pm = mid₃₆₀·(1 + hs/1e4)`.
  Afternoon FILL iff `max(h, minutes 360..389) ≥ ask_pm` → fill at `ask_pm`.
- If still unfilled: forced MOC → `close·(1 − hs_c/1e4)`.
- Baseline realized sell = `close·(1 − hs_c/1e4)`.
- improvement_bps = `(fill_price − close·(1−hs_c/1e4)) / close · 1e4` for a
  morning/afternoon fill; **0** for no-fill (forced MOC = baseline).
- Adverse selection captured automatically via the realized close per filled
  session (you fill on up-moves — did the close end even higher?).
- ORACLE-sell (upper bound, diagnostic): fill at `max(h, 5..10)` (morning) /
  `max(h, 360..389)` (afternoon). Hindsight — never an acceptance bar.

### BUY side — reported under BOTH models (the operator's hybrid is MARKETABLE; ALL-PASSIVE is the genuinely-new alternative)
Baseline buy cost = `close·(1 + hs_c/1e4)`. improvement_bps =
`(close·(1+hs_c/1e4) − buy_cost) / close · 1e4` (lower buy cost = positive).

- **MARKETABLE (operator's hybrid spec; pay the spread, only lever = timing):**
  - NAIVE (realizable, un-timed): `cost = vwap(15..30)·(1 + hs/1e4)`,
    vwap = mean vw over the window.
  - ORACLE (upper bound): `cost = min(vw, 15..30)·(1 + hs/1e4)` (timed dip).
  - improvement ≈ `(close − mid)/close·1e4 − (hs − hs_c)` — flat drift (~0)
    minus the incremental spread PAID. This leg is the *mirror of the parked
    longs-open-exit* → expected DEAD; doubles as the calibration check.
- **PASSIVE (all-passive alternative; earn the spread, force-fill at 10:00):**
  - `bid = mid₁₅·(1 − hs/1e4)`; FILL iff `min(l, 15..30) ≤ bid` → fill at `bid`.
  - unfilled → forced market at 10:00 → `vw₃₀·(1 + hs/1e4)`.
  - improvement (fill) = `(close·(1+hs_c/1e4) − bid)/close·1e4`; (no fill) =
    `(close·(1+hs_c/1e4) − vw₃₀·(1+hs/1e4))/close·1e4`. Same KILL-only caveat as
    passive sells (bars over-fill, can't see adverse selection).

## 2. Statistic

Per (ticker, session): sell improvement, buy improvement. Cluster by SESSION:
per-session mean → across-session mean → t = mean/(sd ddof=1/√n_sessions).
Report SELL (passive naive+oracle), BUY under BOTH models (marketable
naive+oracle; passive naive+oracle), and two COMBINED views — HYBRID
(passive-sell + marketable-buy) and ALL-PASSIVE (passive-sell +
passive-buy) — for each hs, plus FILL RATES (sell morning / sell afternoon /
sell total; passive-buy fill rate). ≥ 600 eligible sessions or INVALID-DATA.

## 3. Pre-committed verdict

**Asymmetric by construction — this instrument can KILL but cannot VALIDATE.**
A minute bar has no bid/ask/queue, so the passive fill rule (`max high ≥ ask`)
mechanically clears a small offset from bar noise on flat days — it *assumes*
spread capture rather than measuring it, and cannot see adverse selection at
the tick/queue level. Therefore:
- A NEGATIVE result (even this over-optimistic model loses net) is a **decisive
  KILL**.
- A POSITIVE sell-side result is **INCONCLUSIVE, not a green light**: bars
  cannot resolve passive provision. It would only motivate a **LIVE SHADOW of
  real passive fills** (or quote/queue data) — never a go-live, never even a
  queue-aware bar-sim (same data limit).

**Calibration check (must pass or the test is untrustworthy):** the marketable
BUY leg must reproduce roughly the spread study's incremental cost (≈ −2 to
−5 bps vs close at realistic hs). The buy leg is the *mirror of the parked
longs-open-exit* — its sign and magnitude are already known. If the test's buy
leg doesn't land there, the machinery is miscalibrated and the sell leg is not
trustworthy either.

- **BUY-side**: expected DEAD (structurally pre-determined: pay morning spread
  for ~0 drift). ORACLE buy reported only to bound the timing prize (needs the
  repeatedly-weak 1b causal edge to capture; not pursued here).
- **SELL-side**: KILL iff naive sell improvement ≤ 0 at every hs; otherwise
  INCONCLUSIVE (report the upper-bound magnitude as the one new number — the
  most a live shadow could hope to realize, before queue/adverse-selection
  discount).
- **HYBRID verdict**: PARK iff COMBINED naive improvement ≤ 0 at realistic hs
  (expected — the buy drag dominates). INCONCLUSIVE-LEAN-SELL iff the sell leg's
  optimistic upper bound exceeds the buy drag → the only thing worth a live
  shadow is *all-passive* (passive buys too), not this hybrid.
- INVALID-DATA iff < 600 eligible sessions.

## 4. Scope / no-peek

Mechanical; progress prints counts only; verdict block = first look. Read-only
on the frozen cache (no master-data writes, no live code). PURSUE authorizes a
queue-aware sim + algorithm design, NOT a go-live. PARK closes the hybrid with
the finality of the marketable spread study.
