# SP-6 Phase B0 — Fill-Persistence + Execution Ledger

**Date:** 2026-06-01
**Status:** Design — approved in brainstorm; pending plan + implementation
**Branch (target):** fast-follow off `feat/sp6-phase-a-eod-open-execution` (do NOT re-open the reviewed Phase-A branch)
**Predecessor:** [SP-6 Phase A design](2026-05-31-sp6-phase-a-eod-open-execution-design.md) (§13 records this as the Phase-B prerequisite)

---

## 1. Problem

Phase B (the alpha-conditioned intraday execution scheduler) grades itself with an
**execution ledger**: `(official close[T+1] − actual_fill) × signed_qty` — the price
improvement of the real fill against the close[T+1] benchmark (design §38, the §28
objective). Today that ledger is **structurally zero forever**:

- The SP-6 open path never persists the real broker fill onto the signal.
- `parity_mark.finalize_parity_marks` overwrites `execution_signals.fill_price` with the
  **official close** (it is the strategy-ledger entry mark, not the broker fill).

The real fill *does* exist — `alpaca_reconcile` populates
`alpaca_submissions.filled_avg_price` / `filled_qty` during the `reconcile` step — but it
is never linked to the signal, and `fill_price` is the wrong (clobbered) place to hold it.

**B0 is the smallest change that stops discarding the real fill** so that, once Phase A is
live, every execution cycle accumulates the ground-truth fill data Phase B needs. It is a
pure data layer: capture + a derived ledger, no consumer/readout.

## 2. Scope

**In scope:** persist the real **entry** fill (price/qty/time) into parity-mark-safe
columns on `execution_signals`, and materialize the entry execution ledger.

**Out of scope:**
- **Exit/close-side slippage** (OPG drop fills vs. close) — that is exit-TCA, deferred to B1.
- **Any consumer/readout** (`#trade-reports` line, dashboard tile) — chosen data-layer-only;
  the readout is more useful once B1 produces non-zero slippage, so it folds into B1.
- The intraday scheduler, the Hawkes signal, and the §28 harness — those are B1/B2.

**Decomposition context:** Phase B = **B0** (this) → **B1** (intraday scheduler, beat-close,
TCA gate, two-bracket split) → **B2** (Hawkes order-flow alpha + §28 weight-gate). B0 is the
only piece that is both a hard prerequisite and fully activation-independent.

## 3. Data model

One additive migration `127_sp6_b0_fill_persistence.sql` adds four **nullable, NO-DEFAULT**
columns to `execution_signals`:

| Column | Type | Meaning |
|--------|------|---------|
| `actual_fill_price` | NUMERIC | real broker VWAP fill for the entry (`alpaca_submissions.filled_avg_price`) |
| `actual_fill_qty` | NUMERIC | real filled qty (handles partial fills + fractional crypto) |
| `actual_filled_at` | TIMESTAMPTZ | broker fill / reconcile timestamp |
| `exec_ledger_usd` | NUMERIC | `(official_close[T+1] − actual_fill_price) × (direction_sign × actual_fill_qty)` |

Additive only → master-DB invariant preserved (columns may be ADDED; nothing dropped).
`fill_price` and `mark_entry_price` semantics are **unchanged** — Phase-A strategy-ledger
parity (`signal_pnl`) is untouched.

## 4. Capture mechanism (inside `finalize_parity_marks`)

The capture lives at the one site that runs at exactly the right moment: 4 PM[T+1], after
the 15:55 into-close cycle's `reconcile` step has populated `alpaca_submissions`, and as part
of the same row UPDATE that already marks the signal FILLED — **before** `fill_price` is
clobbered with the official close.

Mechanism (mirrors the module's existing `held` dict pattern):

1. After the no-rows guard, fetch the day's submissions **once**:
   `SELECT strategy_id, ticker, filled_avg_price, filled_qty, reconciled_at, submitted_at,
    broker_status FROM alpaca_submissions WHERE run_date = target_date`
   into a normalized dict `subs[(strategy_id, norm_ticker)] → (avg, qty, ts)`, using the same
   `_norm_ticker` that normalizes BTC-USD vs BTC/USD. The key is `(strategy_id, ticker)` —
   **not ticker alone** — because `alpaca_submissions` is UNIQUE`(run_date, strategy_id,
   ticker)`, so two strategies trading the same ticker the same day are distinct fills.
   (`alpaca_submissions` has no `workspace_id`; that triple is already globally unique, and the
   marked rows are workspace-scoped on the `execution_signals` side.) Only rows with a real fill
   (`broker_status IN ('filled','partial')`, or `filled_avg_price` present & finite) are kept.
   The per-row SELECT in the existing loop must be extended to also pull `strategy_id` (it
   currently selects only `id, ticker, direction, entry_price, stop_loss, target_1`).
2. Per marked row (already passing the broker-held cross-check + direction/NaN guards), look
   up `subs[(strategy_id, _norm_ticker(ticker))]`. Compute:
   - `actual_fill_price = _safe_float(avg)`
   - `actual_fill_qty   = _safe_float(qty)`
   - `actual_filled_at  = ts` (reconciled_at, else submitted_at — `alpaca_submissions`
     has no broker `filled_at`; reconciled_at is the post-reconcile fill-confirmation time)
   - `exec_ledger_usd   = (mark_price − actual_fill_price) × (direction_sign × actual_fill_qty)`
     — only when both `actual_fill_price` and `actual_fill_qty` are non-NULL finite; else NULL.
     `mark_price` is the official close already computed in the loop; `direction_sign` is the
     `±1` already resolved by `_signal_to_long_short` two lines up.
3. **Extend the existing single `UPDATE`** to also set the four new columns. No second UPDATE,
   no new broker call, no execution-hot-path coupling.

The join key is `(execution_signals.target_date = alpaca_submissions.run_date,
strategy_id, ticker)`; for an SP-6 open executed at 15:55 on T+1, the submission's `run_date`
*is* the signal's `target_date`. (Lookup is done in Python via the normalized dict rather than
a SQL JOIN, to handle crypto symbol normalization consistently with the existing `held` set.)

**Plan-grounding verification (load-bearing assumption):** this join assumes the production
SP-6 sizer records each open's `alpaca_submissions` row under the **signal's own** `strategy_id`.
Confirm during plan grounding that the sizer/executor does not net per-ticker across strategies
into a single submission under a synthetic/aggregate `strategy_id` — if it does, the join must
fall back to ticker-only keying (accepting that same-ticker multi-strategy fills share one VWAP).
A missed match is *non-fatal* (yields `actual_fill_* = NULL`, not a crash), but would silently
leave the ledger empty, so this must be verified, not assumed.

### Sign convention (load-bearing — documented to prevent re-derivation drift)

`exec_ledger_usd > 0` ⟺ the entry fill **beat** the close[T+1] benchmark:

- **LONG** (`direction_sign = +1`): filled *below* close → `(close − fill) > 0`, `signed_qty > 0` → `+`.
- **SHORT** (`direction_sign = −1`): filled *above* close → `(close − fill) < 0`, `signed_qty < 0` → `+`.

This matches the §28 beat-close objective. Because the codebase has been bitten by divergent
sign/qty re-derivation (sharpe_cadence field-shape drift; direction_sign parity), the ledger is
**materialized once** here so B1's TCA and the §28 grader read a single canonical number.

## 5. Edge cases & invariants

- **No submission / no fill / NULL or non-finite avg** → `actual_fill_* = NULL`,
  `exec_ledger_usd = NULL`. A held-but-not-re-entered carry row is the normal NULL case.
- **Partial fill** → `actual_fill_qty = filled_qty` (actual), `actual_fill_price` is its VWAP;
  ledger uses the actual filled qty.
- **Fractional crypto** → NUMERIC throughout (migration 119 widened `alpaca_submissions.qty`).
- **1:1 join** — `alpaca_submissions` is UNIQUE`(run_date, strategy_id, ticker)`; no fan-out.
- **Idempotent re-run** — same inputs → same `actual_fill_*` / `exec_ledger_usd`; `entry_price`
  is unchanged so the existing bracket re-anchor stays stable.
- **Gate-off inert** — `finalize_parity_marks` runs only in the gated SP-6 4 PM block
  (`OPENCLAW_EOD_SIGNAL_REGISTER` path). With SP-6 gates OFF (current production), B0 captures
  nothing and the columns sit NULL. **B0 is therefore byte-identical-when-off and safe to merge
  / deploy independently of Phase-A activation** — it begins capturing the moment Phase A goes
  live.
- **Phase-A parity unchanged** — `fill_price` still = official close; `signal_pnl` unaffected.
- **Phase-A note (expected ~0 ledger):** in Phase A the fill is *into the close*, so
  `actual_fill ≈ official_close` and `exec_ledger_usd ≈ 0` by construction. B0's value in Phase A
  is proving the plumbing end-to-end and starting the fill history; non-zero slippage appears only
  once B1's intraday scheduler replaces the naive into-close fill.

## 6. Testing (TDD)

`tests/test_parity_mark_fill_capture.py` (stub `broker_loader` + an injected submissions
fetch, no live Alpaca):

1. LONG filled below close → `exec_ledger_usd > 0`, columns populated.
2. SHORT filled above close → `exec_ledger_usd > 0`.
3. LONG filled above close → `exec_ledger_usd < 0` (sign sanity).
4. No matching submission → `actual_fill_* = NULL`, `exec_ledger_usd = NULL`, row still marked FILLED.
5. Partial fill → `actual_fill_qty = filled_qty`; ledger uses it.
6. Fractional crypto qty (BTC-USD vs BTC/USD normalization) → captured correctly.
7. **Regression:** `fill_price` still = official close; not-held rows still skipped; bracket
   re-anchor unchanged (parity untouched).
8. Idempotent re-run → identical values.

Plus the existing `parity_mark` regression suite must stay green.

## 7. Rollout

Additive + gate-off-inert, so low-risk:

1. Build on a fast-follow branch off `feat/sp6-phase-a-eod-open-execution` (TDD,
   subagent-driven). Do **not** modify the reviewed Phase-A branch.
2. Apply migration 127 to the live DB (additive/idempotent — safe anytime, like 126).
3. Merge into the Phase-A branch (or stack a PR) — it is inert until SP-6 activates.
4. After Phase A activates and runs a full cycle, spot-check: held opens have
   `actual_fill_price` populated and `exec_ledger_usd ≈ 0` (into-close fill).

No service restart is required for the columns to exist; the capture code path only executes
under the already-operator-gated SP-6 4 PM block.

## 8. Phase-B handoff

B0 delivers the measurement substrate. **B1** (intraday scheduler) reads `exec_ledger_usd` as
its optimization objective and is the first consumer that drives it non-zero; it also owns the
exit-side TCA and the two-bracket split. **B2** (Hawkes alpha + §28 gate) reads the accumulated
ledger as ground truth for alpha-randomization + replay + OOS before `w_hawkes` lifts above 0.
