# SP-6 Phase A — Re-timed EOD→Open Execution Cycle (Design)

- **Date:** 2026-05-31
- **Status:** Design — approved in brainstorming, pending spec review → implementation plan
- **Parent project:** SP-6 *Alpha-Conditioned EOD→Open Execution* (Phase A of A/B/C)
- **Author:** BotJohn (brainstorming session 2026-05-31)

---

## 1. Objective

Re-time the live execution cycle from **same-day-close** (today's `OPENCLAW_CLOSE_EXEC_LIVE` 3:10/3:55 model) to **compute-at-close[T] / act-next-day[T+1]**, and give each signal an **overnight lifecycle** that survives the calendar boundary. Phase A delivers the entire part-1 workflow with a **naive into-the-close fill** (the execution intelligence is Phase B). It is independently shippable and parity-exact even if Phase B never lands.

### What Phase A is NOT
- No intraday "optimal entry" scheduler (Phase B).
- No Hawkes / order-flow signal (Phase B).
- No execution-quality ledger beyond a zero-width stub (Phase B introduces it).

---

## 2. Locked decisions (from brainstorming)

1. **v1 scope:** integrated build; the low-dim Hawkes layer is co-developed in Phase B but `w_hawkes` defaults 0 and lifts only under §28 (alpha-randomization + replay + OOS). *(Phase B/C.)*
2. **Benchmark:** close-anchored — beat `close[T+1]`; passive fallback = fill into the close = reproduce the backtest trade.
3. **Deadline:** fixed at `close[T+1]`; sᵢ + flow shape the participation curve front-loading within that window. *(Phase B.)*
4. **Cycle (part-1):** signals computed at 4 PM[T] on EOD prices (identical to backtest), registered + carried to T+1 iff not rejected by the pre-market news/sentiment gate; at 9:30[T+1] reconcile vs the book; positions held through after-market; reconcile only at the next 9:30.
5. **Asymmetry (confirmed):** full closes of **dropped** signals happen **at the 9:30 open** (OPG); **partial reduces (resize-down)** go through the execution algo (Phase A = into-close fill); same-size persists hold; new/larger enter via the algo.
6. **Zero target signals ⇒ flatten everything** is desired behavior, guarded by a **pipeline-HEALTH** assertion (not a min-signal-count gate).
7. **9:30 closes target live-account OPG semantics**, with a paper-mode `opg_then_day` fallback (OPG at the open + a 9:31 `tif=day` sweep on unfilled). OPG is in **from the start** (operator decision 2026-05-31).

---

## 3. The two-ledger parity model (load-bearing)

A fill *before* `close[T+1]` puts the position on early, so live holds the `fill→close[T+1]` intraday segment the backtest never models (backtest enters *at* `close[t+1]`, exits from t+2). A better entry price is therefore **not** a strict improvement — its sign depends on T+1 intraday drift. Resolution = two explicit ledgers:

- **Strategy ledger** (`signal_pnl`): entry marked at **official `close[T+1]`**, brackets re-anchored to `close[T+1]` via the backtest's own `_reanchor_bracket`, exit walk from T+2. → byte-matches the t+1 backtest **on entries and stop/target exits** → parity exact *there*, promotion Sharpe stays honest, **no intraday backtest simulator needed for v1**. (Signal-drop / flatten exits are a documented live-only divergence — see §3.1.)
- **Execution ledger** (new, Phase B): `(official close[T+1] − actual_fill) × signed_qty`. Absorbs both the price improvement and the intraday-segment exposure. **This is exactly what §28 grades.**

**Invariant:** strategy entry is marked at `close[T+1]`, **never** at the actual fill. (Verified: `engine.py:update_pnl` (936) computes `signal_pnl` off `execution_signals.entry_price`, the frozen decision price, and no path feeds the broker fill into strategy returns. Today entry is marked at `close[T]`; marking at `close[T+1]` is a real but localized change.)

In **Phase A** the fill is *into the close*, so `actual_fill ≈ official close[T+1]` and the execution ledger is **zero-width** — parity is exact for free. The mechanism (mark at `close[T+1]`, re-anchor brackets, entry-date = T+1) is still built in Phase A so Phase B inherits it.

### 3.1 Exit-semantics divergence (foundational — what *is* a trade?)

Two exit models now coexist and they do **not** coincide:
- **Backtest / strategy ledger** (`update_pnl`): a trade exits on **stop / target / max-hold** via the daily-close walk; entry→exit is a discrete per-signal bracket.
- **Live** (reconcile): a position persists day-to-day while its signal is re-emitted, and exits when the **signal drops from the next set → close at the open** (or flatten).

A momentum signal re-emitted for 8 days is *one persistent position* live, but the backtest may exit it on a stop/target on day 3 at a close. **Entry parity is exact; the signal-drop exit trigger and price are a live-only divergence the backtest cannot model** (modeling it would need a portfolio-persistence backtest — a bigger change, deferred). **Phase A resolution:**
1. The strategy ledger records signal-drop/flatten as an **explicit exit**: `close_reason='signal_dropped'|'flattened'`, `closed_price = open-fill price`, `realized_pnl_pct` off `mark_entry_price`.
2. `update_pnl` **stops marking** a position once it is closed-at-open — fixes the **phantom-row bug** (today it closes only on stop/target, so an orphan-closed position keeps accruing phantom unrealized P&L on a position that no longer exists; latent today, *amplified* by daily signal-drop reconciliation).
3. **Parity scope (stated, not assumed):** exact on **entries + stop/target exits**; **signal-drop & flatten exits are a documented live-only divergence.** The intraday-simulator de-scope (§3) rests only on the execution-quality/entry axis, which this does not touch.

---

## 4. Architecture & daily clock (all ET, Mon–Fri)

| When | Step | Built on | New / gate |
|---|---|---|---|
| **T, 4:00 PM** (post-close) | **EOD compute** — `daily-cycle.js` subset `[collect, sentiment, signals]`, reason `eod-signal-register`, on the **real** `close[T]` (no close-proxy). Writes T+1 target set to `execution_signals` at `lifecycle_state='COMPUTED'`. Runs **after** the EOD price append. | `engine.py:run_strategies`→`write_signals` (746–888) | cron `0 16 * * 1-5` (after refresh ~16:05); `OPENCLAW_EOD_SIGNAL_REGISTER` |
| **T+1, 7:15–9:00 AM** | Existing pre-market stack (EDGAR 8-K, panic scanner, sentiment) unchanged. | `run_premarket_scan.py`, `edgar_8k.py` | — |
| **T+1, ~9:15 AM** | **Carry-forward gate** — read T's `COMPUTED` signals; apply panic/sentiment verdict → `APPROVED` or `REJECTED`; persist `signal_gate_verdicts`. | `premarket_panic_alerts`, `tradejohn_confirmer` (**adapt**, not wire — see §6.1) | `OPENCLAW_EOD_PREMARKET_GATE` |
| **T+1, 9:30 AM** (open) | **Reconcile** — diff `APPROVED` set vs book: drops → close at open (OPG dual-path); zero-approved (healthy) → flatten; new/persist/resize → `EXECUTING`. | `regime_blended_sizer.py` delta/orphan (421–480) | `OPENCLAW_EOD_RECONCILE` |
| **T+1, ~3:55 PM** | **Phase-A naive fill** — new/resized entries fill into `close[T+1]` (parity anchor; zero-width exec ledger). | existing close-exec execute path | (Phase B replaces) |
| **T+1, 4:00 PM** | EOD compute again → T+2 set; **finalize the parity mark** for positions filled that day (set `mark_entry_price`, re-anchor brackets to `close[T+1]`). | — | — |

**Dependencies:** (1) supersedes the legacy 3:10/3:55 same-day close-exec for routed strategies (mutually exclusive — see §8); (2) requires the **t+1 backtest branch merged** so backtest fills `close[t+1]`.

---

## 5. Overnight signal state machine

Today signals are fresh each morning with no overnight identity. Phase A gives each a lifecycle on `execution_signals` (additive migration; the silent-strip landmine is a `manifest`/`StrategyRecord` concern, **not** `execution_signals` — we avoid it by not threading new top-level manifest fields).

```
COMPUTED ──(gate pass)──> APPROVED ──(9:30 reconcile)──> EXECUTING ──(fill)──> FILLED
   │                                                          │
   └──(gate veto)──> REJECTED               (drop @ open / flatten) ──> CLOSED_AT_OPEN
```

**New columns on `execution_signals`** (migration 12x — all nullable/defaulted):
- `lifecycle_state TEXT DEFAULT 'open'` — the new enum (legacy `status` untouched for back-compat)
- `computed_at / approved_at / executing_at / filled_at TIMESTAMPTZ`
- `target_date DATE` — the T+1 the signal is *for* (distinct from `signal_date`=T)
- `gate_verdict JSONB`
- `fill_price NUMERIC` — actual broker fill
- `mark_entry_price NUMERIC` — the `close[T+1]` strategy-ledger mark

**New table `signal_gate_verdicts`** (FK→`execution_signals`): `gate_type`, `verdict (approved|rejected|scaled)`, `model`, `metadata JSONB`, `actor`, `decided_at` — so a rejection is audited even when the signal never reaches the broker.

**Carry query** (reconcile at 9:30): `SELECT … WHERE target_date = today AND lifecycle_state='APPROVED'`.

---

## 6. 4 PM EOD compute + the parity mark

- Cron `0 16 * * 1-5` → `daily-cycle.js` `requestedSteps=[collect, sentiment, signals]`, `reason='eod-signal-register'`. **No trade/alpaca** — compute + register only. Runs after the EOD price append (real `close[T]`, no `OPENCLAW_CLOSE_PROXY_SNAPSHOT`).
- `write_signals` writes `COMPUTED` rows: `signal_date=T`, `target_date=T+1`, `entry_price=close[T]` (the strategy's `ref`).
- **Parity mark** (finalized at the 4 PM[T+1] EOD step, when `close[T+1]` is known): for every position filled that day — (1) `mark_entry_price = official close[T+1]`; (2) re-anchor `stop_loss/target_1` via `_reanchor_bracket(ref=close[T], entry=close[T+1], …)`; (3) exit-walk basis = `target_date=T+1` (walk from T+2).
- **Two touches to `engine.py:update_pnl`**: (1) prefer `mark_entry_price` (and `target_date`-based `days_held`) over `entry_price`/`signal_date` when present → its daily-close exit walk reproduces the t+1 backtest exactly; (2) **skip any signal already in `CLOSED_AT_OPEN`** so a dropped/flattened position is never re-marked (the §3.1 phantom-row fix).

### 6.1 The pre-market gate is an *adaptation*, not a wiring job

The existing panic scanner (`run_premarket_scan.py`) scores **held broker positions** (7:30/9:00 scans of the open book). The carry-forward gate must score **carried-signal tickers we do not hold yet** — the `COMPUTED` set's tickers. Different input universe, same scoring core (`panic_score` + FinBERT + optional Sonnet confirmer). The plan treats it as adapting the scorer to the carried-signal universe, not re-pointing the existing scanner.

---

## 7. 9:30 reconcile, flatten & OPG

**Reconcile driver** (new step, 9:30) reuses `regime_blended_sizer.py`'s delta/orphan classifier (421–480):
1. Load `APPROVED` carried set (`target_date=today`).
2. Load broker book (`alpaca position list`) — **re-fetch immediately before any close submit** (staleness guard).
3. Classify per ticker: **DROP** (in book, not in set) → close at open; **NEW** (in set, not book) → `EXECUTING` (Phase A: into-close fill); **RESIZE** (size Δ) → direction-signed delta (Phase A: into-close fill); **HOLD** (same) → no order.
4. **Zero approved + book non-empty + pipeline healthy ⇒ FLATTEN** at the open.
5. **Strategy-ledger close (§3.1):** every executed DROP/FLATTEN also transitions its `signal_pnl`/`execution_signals` row → `CLOSED_AT_OPEN` (`close_reason='signal_dropped'|'flattened'`, `closed_price = open fill`, realized off `mark_entry_price`), and `update_pnl` skips it thereafter — closing the phantom-row gap.

**Asymmetry:** only **DROPs and FLATTEN close at the open** (OPG); **RESIZE-downs are partial reduces → through the algo** (Phase A = into-close fill), never the open.

**Flatten safety = pipeline-HEALTH assertion, not a count gate.** The 4 PM compute writes `eod_compute_health {date, rc, n_strategies_ok/total, regime_ok, universe_size}`. Reconcile honors zero-targets **only if health GREEN**; RED/missing → **abort, preserve, alert**. This is the precise line between "strategies legitimately went flat" (flatten) and the 2026-05-22 empty-signals blowout (preserve).

**OPG dual-path** (drops + flatten), in from the start, selected by `OPENCLAW_OPEN_CLOSE_MODE`:
- `opg_then_day` *(paper default)* — `tif=opg` at the open, **poll to terminal**, then **9:31 sweep**: unfilled → cancel + resubmit `tif=day`. Directly answers the 2026-05-18 (214/224 expired) failure.
- `opg_live` *(live cutover)* — auction-cross OPG, dual-path retained.
- `rth_market` *(safe fallback / kill option)* — plain 9:30 RTH market close.
- **Never ack=fill**: poll to {filled|canceled|expired|rejected} before writing `CLOSED_AT_OPEN` (the `result_status` overload lesson).

**Concurrency:** reuse/extend `execute:close:inflight:{date}` Redis lock; reconcile and the `*/5` intraday redeploy check-and-defer.

---

## 8. Error handling & failure modes

| Failure mode | Guard |
|---|---|
| Pipeline failed vs. legitimately flat | `eod_compute_health` GREEN required to flatten; else abort + preserve + alert. |
| **⚠️ Premarket gate crashed → no `APPROVED` rows → reconcile reads "zero targets" → would flatten** | Flatten **also** requires a **gate-ran sentinel**. Gate-not-run ⇒ **fail-open: promote `COMPUTED`→`APPROVED` + loud alert** — never read "gate never ran" as "everything dropped." Sharpest edge of OPG-from-start. |
| Gate ran but FinBERT :7872 / feed down | Fail-open (approve + alert), configurable. The gate can only veto, never invent approvals. |
| OPG ack ≠ fill (paper) | `opg_then_day` poll-to-terminal + 9:31 day sweep; state/audit only on confirmed terminal status. |
| Stale broker snapshot | Re-fetch positions immediately before open-closes. |
| Re-run / coid collision | Reason-tagged coid namespace for the reconcile; `already_executed()` idempotent skip. |
| Overnight gap inverts brackets | Reuse the backtest's `_reanchor_bracket` (pct-shape preservation) — exact same helper. |
| Strategy de-approved overnight (4 PM→9:30) | Default: honor the validly-computed carried signal; hard-decommission ⇒ implicit drop. *(Edge case.)* |
| Concurrency (reconcile vs 3:55 fill vs `*/5` redeploy) | `execute:close:inflight:{date}` lock; check-and-defer. |

---

## 9. Gates, rollout & test plan

**Gates (default-OFF):** `OPENCLAW_EOD_SIGNAL_REGISTER`, `OPENCLAW_EOD_PREMARKET_GATE`, `OPENCLAW_EOD_RECONCILE`, `OPENCLAW_OPEN_CLOSE_MODE ∈ {rth_market, opg_then_day(paper default), opg_live}`.
**Mutual exclusion:** flipping the EOD flow ON requires `OPENCLAW_CLOSE_EXEC_LIVE` OFF — they can't both drive routed strategies. Doctor enforces.

**Rollout (reversible, gated):**
1. **Dependency:** merge the t+1 backtest branch → backtest fills `close[t+1]`.
2. Ship Phase A, gates OFF.
3. **Shadow** (days): 4 PM compute + carry + gate in register-only mode; verify carried `APPROVED` set, state machine, health sentinel on live data — **zero orders**.
4. **Dry-run** the 9:30 reconcile (`--dry-run`): log drops/flatten/holds vs the real book; validate diff + flatten-health guard.
5. **OPG paper spike** (small): confirm `opg_then_day` fills or falls through to the 9:31 sweep.
6. Flip gates with operator present; simultaneously disable legacy close-exec.

**Test plan:**
- **Unit:** state transitions incl. `CLOSED_AT_OPEN`; carry query; `mark_entry_price` in `update_pnl`.
- **Parity (suite the architecture map flagged as *missing*):** take a Phase-A trade, re-simulate in backtest with same `target_date` → assert identical exit date/price within tolerance (`signal_pnl == backtest trade`).
- **Flatten guard:** GREEN+zero→flatten; RED→preserve; **gate-not-run→fail-open promote (no flatten)**.
- **OPG 2026-05-18 replay (core test):** 224 closes, simulated paper-OPG expiry → `opg_then_day` → assert 9:31 sweep closes the unfilled, terminal-status audit correct.
- **Reconcile diff:** drop/new/resize/hold; resize-down→close-fill (not open); only drops+flatten at the open.
- **Concurrency:** inflight lock blocks reconcile↔redeploy collision.
- **system_checks/doctor:** `eod_compute_health` freshness, `carried_set_present`, `gate_ran`, `reconcile_fired`; doctor preflight for new crons + mutual-exclusion gate check.

---

## 10. Dependencies & out-of-scope

**Dependencies:** t+1 backtest branch merged (parity); existing close-exec model (superseded, mutually exclusive); pre-market scanner + sentiment confirmer (**adapted** to score the carried-signal universe — §6.1).

**Out of scope (Phase B):** the alpha-conditioned scheduler (9:30→close participation curve, beat-close objective, TCA gate, liquidity sizing, emergency overrides); the low-dim Hawkes signal + §28 weight-gate; the non-zero execution ledger; the **two-bracket-set divergence** (broker protective stop/target placed at fill vs. the `close[T+1]`-re-anchored strategy-ledger brackets — they coincide in A because fill≈close, but split in B). **Out of scope (Phase C):** activation/validation/rollout of B; paper→live OPG cutover.

---

## 11. Open questions / edge cases
- **[FOUNDATIONAL — resolved in §3.1, confirm before plan]** *What is a "trade" in the new world?* — signal-presence-keyed (persistent until dropped, live) vs stop/target-keyed (per-signal bracket, backtest). Phase A adopts: strategy ledger records **both** exit types; signal-drop/flatten is an explicit live-only exit; parity is exact on entries + stop/target exits only. Confirm this framing — it sets the data model.
- Strategy de-approved between 4 PM[T] and 9:30[T+1]: honor vs implicit-drop (default honor) — confirm.
- `opg_live` belt-and-suspenders: keep the 9:31 day sweep even on live, or trust the auction cross?
- Resize-down timing: confirmed it goes through the algo (into-close in A), not the open — restate in the plan so it isn't mis-built as an open-close.
