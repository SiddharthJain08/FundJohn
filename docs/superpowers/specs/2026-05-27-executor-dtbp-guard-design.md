# Executor DTBP Guard + Buying-Power Monitoring — Design

**Date:** 2026-05-27
**Status:** Design (awaiting operator review → writing-plans)
**Scope:** Prevent the daily executor from over-submitting opening orders beyond the account's available day-trading buying power, and instrument buying-power state for monitoring. Executor-only change.

---

## 1. Background — what triggered this

On 2026-05-27 the 10 AM cycle had **8 orders fail to fill**, and the targeted notional looked too high. Investigation (read-only) found:

- All 8 failures were **new long bracket opens** rejected with HTTP 403 **`insufficient day trading buying power`**.
- The account (PAXXXXXXXXXX) is PDT, `multiplier=4`, with **`daytrading_buying_power = 0`**, `regt_buying_power ≈ $58k`, equity ≈ $110.6k.
- The sizer (`regime_blended_sizer.py`) targets `Σ|target_usd| = λ × equity` with **no buying-power awareness**; the executor (`alpaca_executor.py`) submits the full set with **no pre-submit BP check** (log: *"pure sizer output — no executor-side cap"*). So when buying power is exhausted, the tail orders are rejected by Alpaca rather than skipped cleanly.

**Root cause of the exhaustion (`daytrading_buying_power = 0`):** chronic day-trade churn. Alpaca reported `daytrade_count = 64` in the trailing 5 business days. The day-trades come almost entirely from **intraday redeploys that round-trip same-day positions** on regime-change days (05-21: 23 round-trip symbols; 05-22: 49), confirmed by fill-time clustering (86% of 05-22 fills landed in the two redeploy waves, not scattered as bracket stop-outs). The **heaviest churn came from after-hours redeploys**, which only ran because the `OPENCLAW_REDEPLOY_EXTENDED_HOURS` gate was **ON** during 05-21/22. That gate is **OFF (`=0`) now** and the RTH clock check is sound, so after-hours equity redeploys are already blocked going forward. Today's rejections are largely **residual** — DTBP is still 0 because those day-trades remain inside the rolling 5-business-day window (they roll off ~05-28/29).

**Two structural gaps remain regardless of the residual recovery:**
1. The executor has no DTBP awareness, so *any* time DTBP is low the tail opens 403 instead of skipping cleanly. (This spec.)
2. RTH redeploys can still re-trade same-day names and deplete DTBP on volatile weeks. (Deferred — see §7.)

**Already done by the operator (not part of this spec):** lowered `position_sizing_lambda` 2.0 → **1.5** in `pipeline_config`, which reduces book size and restores ~$55k of Reg-T headroom.

---

## 2. Goal / non-goals

**Goal:** the daily executor never submits an opening order it can't fund. When day-trade/Reg-T capacity is exhausted, it deploys what fits (highest-conviction first) and **cleanly skips the lowest-conviction remainder** — no Alpaca 403s, clean operator reporting. Plus capture buying-power state each run so we can confirm DTBP recovery.

**Non-goals:**
- No sizer math changes (λ=1.5 already handles leverage appetite).
- No RTH-redeploy churn reduction (Gap 2 — revisit only if monitoring shows DTBP staying depleted).
- No re-enabling of extended-hours redeploys.
- No change to closing/reducing behavior — those free buying power and must always submit.

---

## 3. The guard

**Location:** `src/execution/alpaca_executor.py`, inside the existing `main()` submit loop (the closes-first `sorted(orders, key=_exec_priority_for_test)` loop). One new helper; no new files, no sizer touch.

**Algorithm:**

1. **Closes-first tiering is unchanged and never guarded.** Tier 0 (orphan closes) and tier 1 (reduces/covers) free buying power → always submitted. The guard applies **only** to opening tiers (2 = short opens, 3 = long opens / increases).

2. **Compute the opening budget lazily, right before the first opening-tier order.** Re-fetch the account once (via `_fetch_account_state(sess)`) at the tier-1→tier-2 boundary, so the budget reflects buying power freed by the closes/reduces already submitted:
   ```
   budget = max(0, min(daytrading_buying_power, regt_buying_power))
   ```
   `min(...)` respects both the intraday day-trade limit (what Alpaca rejected on) and the Reg-T overnight limit. **No headroom factor** (operator decision 2026-05-27): the guard deploys to the exact edge.
   *Fill-race note:* closes are submitted sequentially via the Alpaca CLI (each ~sub-second), so by the time the loop reaches the opens most close orders have already filled and their freed BP is reflected in the re-fetch. Any close not yet filled simply makes the budget conservative (under-deploy), never over-submit. The plan may optionally poll close fills before the re-fetch if the race proves material.

3. **Order opens by conviction, submit until the budget is spent, skip the rest.** Within the opening tiers, sort by conviction **descending** (`kelly_final`, falling back to `pct_nav`). Use the executor's existing notional basis `notional = equity × pct_nav` (same value as the loop's current `projected`, and robust to the `notional_usd = NULL` seen on rejected rows). Maintain a running `remaining = budget`; for each open, if `notional <= remaining`, submit and decrement; otherwise **skip it and all lower-conviction opens**, tagged `skipped_dtbp`. (This refines the current largest-notional-first within-tier ordering, whose "free BP early" rationale only applies to the close tiers.)

4. **Result:** the cycle deploys exactly what capacity allows, highest-conviction first; the remainder is skipped, not rejected.

**Edge case (accepted):** with no headroom, the single marginal open at the budget edge can still 403 if its fill price ticks above the guard-time estimate. That is at most one edge order (vs today's eight) and rare with λ=1.5 leaving Reg-T room. If monitoring shows recurring edge rejects, re-introduce a small buffer then.

**DTBP = 0 case:** budget = 0 → all opens skipped, all closes/covers still fire. This is correct (no day-trade capacity) and surfaces clearly in reporting; it is the honest "we're capacity-starved today" signal rather than a wall of 403s.

---

## 4. Reporting

- Skipped opens get an `alpaca_submissions` row with `alpaca_status = 'skipped_dtbp'`, `alpaca_order_id = NULL`, so `already_executed()` retry semantics still treat them as not-executed (a later cycle with restored capacity can pick them up).
- The existing `_post_executor_summary` (#trade-reports) gains a line: *"deployed N, skipped M for buying-power (~$X, lowest-conviction)"* — a clean operator signal instead of a partial-failure alarm.

---

## 5. Monitoring instrumentation

At the start of every executor run, append one row to `logs/bp_snapshots.csv` (append-only): `timestamp, run_date, equity, daytrading_buying_power, regt_buying_power, daytrade_count, multiplier`. This lets us watch DTBP recover over the next ~3 sessions as the 05-21/22 day-trades roll off the rolling window, and is the signal that decides whether Gap 2 (RTH churn) ever needs work. (`daytrade_count` and the BP fields all come from the Alpaca account object already fetched at run start.)

---

## 6. Config

- **`OPENCLAW_DTBP_GUARD`** — kill-switch, **default ON** (absent variable = guard active). Shipped on, per operator decision; retained as an off-switch so the guard can be disabled without a redeploy if it ever misbehaves. Guard-OFF path is byte-identical to today's executor.

No headroom config (removed by decision).

---

## 7. Out of scope / deferred

- **RTH-redeploy churn reduction (Gap 2):** revisit only if `bp_snapshots` shows DTBP failing to recover after the gate-ON day-trades roll off. Candidate levers if needed: skip rebalancing names opened the same day; cap redeploys per rolling window.
- **Extended-hours redeploys:** stay gated OFF; re-enabling needs real day-trade budgeting (arguably SP-5 territory).
- **Stale SPY option positions** (`SPY260618C00750/760`): not from the pipeline (no `alpaca_submissions` rows); eating margin now. Separate operator flatten — not this change.
- **Sizer changes:** none; λ=1.5 stands.

---

## 8. Testing

Unit tests with a fabricated account state + order list:
1. Tight budget → skips the correct **lowest-conviction** opens; higher-conviction opens submit.
2. Closes/covers (tier 0/1) are **never** skipped, even at budget 0.
3. Conviction-descending submission order within open tiers.
4. `budget = min(DTBP, regt_bp)` selection (DTBP-binds case and regt-binds case).
5. **Guard OFF → byte-identical** to current behavior.
6. DTBP = 0 → all opens skipped, all closes fire, summary reports the skip count.
7. `skipped_dtbp` rows written with NULL order id and don't block later retry.

Regression: existing executor tests (bracket recompute, ext-hours, flip pairs, reconcile) stay green.

---

## 9. Rollout

1. Implement in a git worktree (per superpowers process), TDD.
2. Land behind `OPENCLAW_DTBP_GUARD` (default ON). Guard-OFF byte-identity test gates the merge.
3. After merge: regenerate the integrity manifest on the VPS; do **not** commit it.
4. Watch `bp_snapshots.csv` + the next 2–3 daily cycles: confirm zero 403s and clean `skipped_dtbp` reporting, and whether DTBP recovers as the gate-ON day-trades roll off.
