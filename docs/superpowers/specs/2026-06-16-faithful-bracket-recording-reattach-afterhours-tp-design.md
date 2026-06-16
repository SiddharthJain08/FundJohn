# Faithful bracket recording + latest-run reattach + after-hours take-profits

**Date:** 2026-06-16
**Status:** Design (approved by operator; pending spec review → plan)
**Origin:** WDC take-profit silently dropped on the overnight reattach (06-15→06-16). The
position rode to a premarket high of 724.49 (above the 717.03 TP that was actually placed) with
no resting take-profit and only a far-below bare stop. Root-cause investigation surfaced three
defects in how protective brackets are recorded and re-established.

---

## 1. Background & failure model

### How orders reach the broker
An open position's live bracket is placed by **whichever run ran last**:
- the **EOD→open lane** (SP-6 Phase A): compute 16:15 ET[T] → fill ~3:55 AM ET[T+1], OR
- a **regime-transition redeploy** (`scripts/redeploy_pipeline.py` → `signals→handoff→trade→alpaca→reconcile`) at an arbitrary RTH time.

Confirmed on 06-15: there was **no 3:55 AM ET pre-open submission burst**; the only full-book
submission was an **18-ticker burst at 10:02 ET (14:02 UTC)** ~30 min after the open — a
regime-transition redeploy. The WDC bracket (TP 717.03 / stop 611.89) came from that redeploy.

### Why protection evaporates overnight
Alpaca bracket exit legs inherit **TIF=day** and are expired/canceled at each RTH close (the OCO
group dies when either leg terminates). `src/execution/stop_reattach.py` is responsible for
re-establishing GTC protection (an OCO take-profit + stop, or a bare-stop floor).

### The three defects
1. **Recording is split-brain (W1).** `execute_single` (`alpaca_executor.py`) computes the
   actually-placed legs (after `stacked_bracket()` combine in the sizer *and* the pre-flight
   `_recompute_bracket_from_quote()` re-anchor) and returns them in its result dict
   (`{'stop': stop, 'target': target, ...}`). But `record_submission` reads `entry` from the
   **result** while reading `stop`/`target` from the pre-submit **`order`** dict
   (`order.get('stop')`, `order.get('t1') or order.get('target')`). So `alpaca_submissions` stores
   per-strategy / pre-recompute levels that do not match the legs the broker accepted. For WDC the
   row recorded `target_price=604.79 ≤ entry_price=627.51` (a degenerate long target) while the
   broker actually held TP 717.03 / stop 611.89.
2. **Reattach silently drops the take-profit (W2).** `stop_reattach.run_oco_reattach` recomputes
   the TP from the (wrong) audit row via `_compute_new_target`. A recorded target ≤ entry returns
   `'degenerate'`; the OCO pass then skips and only the **bare-stop floor** fires — a bare GTC stop
   with **no take-profit**, no operator signal. (Reproduced live:
   `OCO pass: {'degenerate': 1, ...}` for WDC.) The recorded stop (516.11 → reattached 516.35,
   ~25% below market) also gutted the downside vs the placed 611.89.
3. **All protection is RTH-only (W3).** Native stop / OCO / bracket orders are not monitored
   outside 09:30–16:00 ET, so a position has no resting protection during pre/post-market.

### Blast radius
Degenerate long-bracket rows (`target_price ≤ entry_price`) span ~5 weeks (since 2026-05-11; 21
rows, front-loaded with 13 that first week). A row repair has **no** operational value (those
positions are closed; nothing rests to fix). Forward correctness + a single W2 reattach pass at
deploy is the remediation. Live TP-less open positions at design time: **MU** (and WDC, being
liquidated by the operator).

---

## 2. Approved decisions

- **Source of truth = the legs the broker accepted.** Record and reattach from the broker's actual
  bracket legs, not any pre-submit field. Robust regardless of the exact upstream transform.
- **W3 v1 = after-hours take-profits only, no monitor.** Resting extended-hours limit TPs placed at
  each session open + a session-boundary reconcile. Downside stays RTH-only. The stop-breach
  monitor is a documented fast-follow.
- **No schema change** (reuse `stop_price`/`target_price`); no `bracket_source` audit column.
- **No historical row repair.**

---

## 3. W1 — Record the bracket the broker actually accepted

**Goal:** `alpaca_submissions.target_price` / `stop_price` / `entry_price` reflect the legs Alpaca
accepted, for *every* originating run.

**Source precedence (highest first):**
1. The accepted bracket legs read from the Alpaca order: `take_profit.limit_price` and
   `stop_loss.stop_price` from the order's nested `legs` (re-fetch the order by id if the submit
   response does not inline them).
2. The `execute_single` result's `target` / `stop` (already the post-recompute placed values).
3. The `order` dict (`order['t1']`/`order['stop']`) — last resort only.

**Rules:**
- A long target ≤ entry (or short target ≥ entry) is **never recorded silently**: log a WARN and
  emit a visible signal (it indicates a degenerate placement upstream).
- Applies to **both** `record_submission` call sites (`alpaca_executor.py:424` and `:2800`); the
  plan pins down which path wrote WDC's row and routes both through the broker-legs source.
- Simple / rejected / extended-hours / crypto orders (no bracket) keep their existing recording
  (no take-profit/stop legs to read) — byte-identical.

**Gate:** `OPENCLAW_RECORD_PLACED_BRACKET` (default-OFF).

---

## 4. W2 — Reattach from the latest run's real orders; never drop a TP

**Goal:** re-establish a GTC OCO (TP + stop) from the levels the last run actually placed; never
leave a profitable position with no take-profit and no operator signal.

**Source precedence (highest first):**
1. The most-recent **terminal** bracket for the symbol from Alpaca order history
   (`order list --status all --nested`) → its real TP/stop leg prices. This is "the latest run's
   intended bracket," independent of audit-row quality, and naturally prefers an intraday redeploy
   over the prior EOD.
2. The (post-W1-correct) latest `alpaca_submissions` row.

**Rules:**
- Re-anchoring preserved: apply the bracket's pct-from-entry shape to the position's current
  `avg_entry_price` (unchanged from today), so averaged-in positions stay consistent.
- **Never silent-drop:** if no valid profit-side target exists in either source
  (degenerate/missing), do **not** place a bare-stop-only and continue. Place the protective stop
  AND surface the missing-TP for operator review (mirror the existing `breached` path: dedicated
  log + Discord alert). The current silent `degenerate → bare-stop-floor` path is removed.
- Idempotent (skip positions already carrying a resting TP), qty-aware, and the existing
  cancel-stop → wait-for-shares → place-OCO sequence is preserved.

**Deploy remediation:** one `stop_reattach --oco` pass at deploy re-establishes TPs on currently
TP-less open positions (e.g. MU).

**Gate:** `OPENCLAW_REATTACH_FROM_BROKER` (default-OFF).

---

## 5. W3 v1 — After-hours take-profits (no monitor)

**Goal:** a take-profit can fill during pre/post-market, not just RTH.

**Why limits, not stops:** Alpaca extended-hours orders must be `limit` + `day` TIF +
`extended_hours=true`. A sell-limit **above** market = a clean ext-hours take-profit (rests, fills
if crossed). A stop cannot be represented in ext-hours (stop/stop_limit are rejected with
`extended_hours`, and a sell-limit at/below market fills immediately). So W3 v1 covers **upside
only**; downside stays on the RTH GTC stop.

**Placement:** at each ext-hours session open — pre-market ~4:00 ET (08:00 UTC) and post-market
~4:00 PM ET (20:00 UTC) — place a resting `limit / day / extended_hours=true` sell at the TP for
each open long (mirror for shorts). Reuses `_submit_order_via_cli(order_type='limit',
extended_hours=True)` and `_pick_limit_price` precedent from the redeploy path.

**Session-boundary reconcile** (ext-hours TP and RTH GTC stop are **not** OCO-linked — OCO is
RTH-only):
- On session transition, cancel the prior session's ext-hours TP (day-TIF would expire anyway;
  cancel makes it deterministic).
- If an ext-hours TP filled, cancel/resize the now-oversized GTC stop (prevent an oversell).
- At RTH open, the normal GTC OCO reattach resumes ownership.
- Idempotent + qty-aware (never place a TP for qty already covered).

**Scheduling:** two new session-boundary timers (pre-market-open, post-market-open) drive the
placer + reconcile.

**Out of scope (fast-follow):** the stop-breach **monitor** that fires a marketable ext-hours limit
when price crosses the stop level — the only way to get after-hours *downside* protection.

**Gate:** `OPENCLAW_AFTERHOURS_TP` (default-OFF).

---

## 6. Rollout & testing

**Staged gates (default-OFF):** flip `OPENCLAW_RECORD_PLACED_BRACKET` (W1) first → soak → flip
`OPENCLAW_REATTACH_FROM_BROKER` (W2) + run one reattach pass → soak → flip `OPENCLAW_AFTERHOURS_TP`
(W3).

**TDD coverage:**
- **W1:** records broker legs over a degenerate `order` dict; degenerate placement is logged not
  silently stored; simple/reject/crypto recording byte-identical.
- **W2:** prefers broker-history legs over the DB row; degenerate/missing target surfaces (not
  silent-drop); WDC-replay reconstructs the 717.03 / 611.89 GTC OCO; re-anchor math preserved;
  idempotent skip when a TP already rests.
- **W3:** placement uses `limit`+`day`+`extended_hours`; reconcile cancels the prior session's TP
  and resizes the GTC stop after an ext-hours fill; no double-cover.
- Regression: existing `tests/test_protective_oco.py` + executor/sizer suites stay green.

**Live verification:** a tiny paper smoke confirming an ext-hours `limit`+`extended_hours` order is
accepted (and that a `stop`+`extended_hours` is rejected, validating the limits-only design) before
W3 flip.

**No schema migration. No master-data mutation.**

---

## 7. Open items for the plan

- Pin which `record_submission` call site wrote WDC's degenerate row (424 vs 2800) and whether
  `order` is mutated between line 2163 (target compute) and the record call — to confirm both paths
  are covered.
- Confirm the Alpaca order-submit response inlines `legs`, or whether W1 needs a follow-up
  `order get --nested` by id.
- Timer/service definitions for the two W3 session-boundary triggers (naming, ET→UTC, DST).
