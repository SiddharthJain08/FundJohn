# SP-6 — Longs-only open-exit structure + Probe ① gate (DESIGN)

Date: 2026-06-08. Status: **DESIGN — pre-registration for the gating probe.**
Supersedes the wholesale open[t+1] fill-model direction (operator call: that is
abandoned — "even if 9:30 submit proves valuable across all strategies,
execution cost will kill any derived alpha excess"). Phase A close-execution
remains the live baseline (it already is; the open-fill study was read-only
research, never wired live).

## 0. The target structure (what this ultimately ships)

Phase A, unchanged, **except signal-driven SELLS/REDUCTIONS of LONG positions
move from the 3:58 PM ET close to the next 9:30 AM ET open.** Everything else
holds:

- Signals computed from EOD prices on day D.
- New positions / increases: execute at the **3:58 PM close** (unchanged).
- Signal-driven exits/reductions of **longs**: execute at the **9:30 AM open**
  the next session.
- Signal-driven exits/reductions of **shorts (buy-to-cover)**: stay at the
  **3:58 PM close** (unchanged).
- Bracket exits (stop/target, ~92% of historical exits) are intraday
  level-crossings — **unaffected** by this rule; they fire whenever hit.

### Why longs-only (the economics, settled in brainstorming)

Against the operator's stated baseline ("hold the to-be-exited name intraday
until the next 3:58 PM"), both arms hold the position across the overnight gap
(close[D]→open[D+1]) — **overnight P&L is identical in both arms.** The *only*
difference is whether you sit through the **D+1 intraday session** on the way
out. So the per-exit edge for a long is exactly **−E[intraday_return]**,
where `intraday_return = (close − open)/open` on the exit day:

- Exit a **long** at the open → skip the session → gain iff intraday ≤ 0.
- Exit a **short** at the open → forfeit a session you'd want to stay short →
  **loss** under the same regime.

The live book is ~42% short (backtest trade mix long 1.26M / short 0.92M), so
a blanket open-exit would systematically hurt nearly half the book. Hence
longs-only.

### Operational guard (carried into the live spec, not optional)

The live book is **paper**. Paper 9:30 fills are a **non-fill** hazard, not
just slippage (history: OPG/MOO ~7% fill; 214/224 closes once expired at the
open cross). The open-exit lane MUST fire **≥9:31 ET as marketable-limit /
TIF=day (never OPG/MOO), with automatic fallback to the 3:58 close if the open
fill does not confirm.** The operator's "friction shrinks live" point covers
slippage, not non-fill — this guard is what makes a backtested edge realizable.

## 1. Probe ① — the gate (pre-registered)

The standard per-trade backtest **cannot** validate this (92% of its exits are
bracket level-crossings, invisible to open-vs-close; it has no "signal dropped
this name" event — that is a portfolio-rebalance concept the live sizer owns).
So we measure the only thing that matters — the exit-day intraday return for
the relevant longs — directly on 10y daily bars.

### 1.1 Quantity & statistic

- `intraday_return(ticker, d) = (close[d] − open[d]) / open[d]` from
  `data/master/prices.parquet`.
- **Clustered statistic M1** (same convention as Test-A): per exit-day d, mean
  intraday_return across the population that day → across-day mean → t = mean /
  (sd[ddof=1] / √n_days). Same-day names are beta-correlated, so the honest n
  is the number of distinct exit DAYS, not rows. M1 is the gating measure
  applied to the PRIMARY and SECONDARY populations below.

### 1.2 Populations

- **PRIMARY (b) — thesis-expired longs:** the (ticker, exit_date) pairs of
  `max_hold` LONG exits from `strategy_backtest_trades` (primary-window runs).
  `max_hold` is the closest backtest analog to "signal no longer holds this
  name" — the live rule's actual trigger. Cluster by exit_date.
- **SECONDARY (a) — broad universe:** all (ticker, day) over 10y in the active
  equity universe. Max power; the unconditional prior ("if dropped longs
  behave like average names").
- **DIAGNOSTIC M2 — within-day relative:** PRIMARY intraday_return minus the
  universe same-day mean. Strips the market component → reveals whether dropped
  *names specifically* underperform intraday vs a market-wide session effect.
  Non-gating.

### 1.3 Buckets

By **regime** (LOW_VOL / TRANSITIONING / HIGH_VOL / CRISIS, from
`historical_regimes.parquet` on the exit_date) and by **calendar half-year**
(recent-regime guard).

### 1.4 Pre-committed verdict (ASYMMETRIC VETO — operator-chosen)

The payoff is asymmetric (gain if the exit session is negative, ~neutral if
flat, loss if positive) and M1 is intrinsically low-power (the absolute
intraday return is small vs daily vol). So the historical probe's job is to
**rule out a positive session**, not to prove a negative — consistent with the
session's historical-can-veto-not-prove discipline. The forward live fills are
the true net-edge arbiter.

- **NO-GO** iff PRIMARY pooled mean intraday_return is reliably **POSITIVE**
  (t ≥ +3), **OR** reliably positive (t ≥ +2) in either of the two
  most-recent half-year buckets. (The session favors holding longs →
  open-exit loses.)
- **CLEAR-TO-SHIP-GATED** iff not NO-GO. If the PRIMARY pooled point estimate
  is > 0 but not significant (t < +3 and the recent-bucket guard not tripped),
  label **CLEAR-WITH-CAUTION** (ship gated, but flag the weak adverse sign;
  watch forward fills closely).
- **INVALID-DATA** iff fewer than **500 distinct exit-day clusters** in
  PRIMARY (else the gate has no power).

### 1.5 No-peek

Mechanical evaluator; one run; the printed verdict block is the first look at
any number. Progress logs emit counts only.

## 2. Decision linkage

- **NO-GO** → do not ship; Phase A close-exit stands for longs; question
  closed. Accrual/forward work unaffected.
- **CLEAR(-WITH-CAUTION)** → proceed to a separate live-structure spec/plan:
  longs-only open-exit on the rebalance lane, default-off gate, the §0
  operational guard, forward-confirm on live fills, operator flips the gate.
  Shorts and entries unchanged throughout.

## 3. Out of scope

- Shorts (stay at close), entries (stay at close), bracket exits (unaffected).
- The full portfolio-rebalance simulator (rejected as multi-week overkill for
  a one-bit directional decision).
- Any live change in THIS spec — it builds and runs Probe ① only.
- Any push to origin / gate flip / service restart (separate operator
  approvals).
- The incremental open-window spread cost: the probe measures gross direction
  only; net cost is ratified by forward live fills (bar-range spread proxy is
  unreliable, per Phase-1e).

## 4. Build surface (for the implementation plan)

- `src/research/exit_timing/intraday_session_probe.py` — pure functions
  (load PRIMARY exit-day set from `strategy_backtest_trades`; join to
  prices.parquet open/close; regime + half-year buckets; clustered-t; verdict).
- `scripts/run_intraday_session_probe.py` — runner: progress = counts only;
  writes `analysis/exit_timing_probe/{report.md, rows.parquet}`; prints
  `[exit-probe] VERDICT: <…>`.
- `tests/test_intraday_session_probe.py` — synthetic worlds: positive-session
  → NO-GO; negative-session → CLEAR; flat → CLEAR-WITH-CAUTION; recent-bucket
  positive → NO-GO; <500 clusters → INVALID-DATA; clustered-t correctness.
- House rules: branch `feat/sp6-phase-a-eod-open-execution`; `git add` explicit
  paths only (never stage manifest.json / strategy_signatures.json /
  run_sentiment_step.py); read-only against master parquet; tests on synthetic
  tmp data only; sequential `nice -19`; the 10y run detached via systemd-run.
```
