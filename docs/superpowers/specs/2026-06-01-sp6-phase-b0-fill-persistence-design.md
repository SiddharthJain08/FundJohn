# SP-6 Phase B0 — Fill-Persistence + Execution Ledger

**Date:** 2026-06-01
**Status:** Design — approved-then-revised in brainstorm (grain fix; see §0); pending plan re-confirm + implementation
**Branch (target):** fast-follow off `feat/sp6-phase-a-eod-open-execution` (do NOT re-open the reviewed Phase-A branch)
**Predecessor:** [SP-6 Phase A design](2026-05-31-sp6-phase-a-eod-open-execution-design.md) (§13 records this as the Phase-B prerequisite)

---

## 0. Revision note (per-order grain)

The first draft stored the ledger as four columns on `execution_signals`, keyed per signal
`(strategy_id, ticker)`. **Codebase grounding invalidated that grain.** The production sizer
(`regime_blended_sizer_live.py`) runs in **consolidate mode** (verified live: on 2026-05-28 every
ticker had exactly one `alpaca_submissions` row, `n_strat=1` per row): N strategies that signal
the same ticker are netted into **one broker order**, recorded under the lead contributor's
`strategy_id`. Multi-strategy tickers are prevalent (STX = 8 strategies, AKAM/AEE = 4, ≥15
tickers with 2+ on a single signal day). Execution happens **per order**, not per signal — you
cannot independently execute two orders for the same symbol. So the execution ledger's grain is
the **order**, and the per-signal model was wrong for the common case (lead strategy would get
the full consolidated qty; the others NULL).

**The fill is also already persisted.** `alpaca_reconcile` writes `filled_avg_price` /
`filled_qty` / `reconciled_at` onto `alpaca_submissions` — which *is* the order grain (one row
per broker order). So B0 is not "rescue a discarded fill"; it is "add the close[T+1] benchmark +
the derived ledger next to the fill that's already there." This makes B0 **smaller**: two columns
on `alpaca_submissions` and one sibling function — no `execution_signals` changes at all.

## 1. Problem

Phase B (the alpha-conditioned intraday execution scheduler) grades itself with an
**execution ledger**: `(official close[T+1] − actual_fill) × signed_qty` — the price
improvement of the real order fill against the close[T+1] benchmark (design §38, the §28
objective). Today that ledger is **never computed and cannot be reconstructed after the fact**,
because the close[T+1] benchmark is not captured alongside the fill: `alpaca_submissions` holds
the real fill (`filled_avg_price`, `filled_qty`) but nothing records the official close the fill
should be measured against, and `parity_mark` only writes the close onto `execution_signals`
(the strategy ledger), not onto the order.

B0 captures the benchmark + materializes the ledger at the **order grain** so that, once Phase A
is live, every execution cycle accumulates the ground-truth data Phase B needs. Pure data layer:
benchmark + derived ledger, no consumer/readout.

## 2. Scope

**In scope:** for each filled **entry** order in `alpaca_submissions`, record the official
close[T+1] benchmark and materialize the per-order execution ledger.

**Out of scope:**
- **Exit/close-side orders** (orphan-closes, SP-6 drop closes) — that is exit-TCA, deferred to
  B1. B0 excludes sentinel strategy ids (any `__`-prefixed id, e.g. `__close_orphan__`).
- **Per-strategy attribution / per-symbol rollup / gross-vs-net** — persist at the order grain
  only; you can always aggregate orders up to a symbol later, never disaggregate a netted symbol
  number back into orders. Rollup is a Phase-B-objective choice.
- **Any consumer/readout** (`#trade-reports` line, dashboard tile) — chosen data-layer-only.
- The intraday scheduler, the Hawkes signal, and the §28 harness — those are B1/B2.

**Decomposition context:** Phase B = **B0** (this) → **B1** (intraday scheduler, beat-close,
TCA gate, two-bracket split, exit-side TCA) → **B2** (Hawkes order-flow alpha + §28 weight-gate).
B0 is the only piece that is both a hard prerequisite and fully activation-independent.

## 3. Data model

One additive migration `127_sp6_b0_fill_persistence.sql` adds two **nullable, NO-DEFAULT**
columns to `alpaca_submissions`:

| Column | Type | Meaning |
|--------|------|---------|
| `official_close` | NUMERIC | official close[T+1] for this order's ticker (the beat-close benchmark) |
| `exec_ledger_usd` | NUMERIC | `(official_close − filled_avg_price) × (direction_sign × filled_qty)` |

The fill itself (`filled_avg_price`, `filled_qty`, `reconciled_at`) is **already** on
`alpaca_submissions` — no new fill columns, no `execution_signals` changes. Additive only →
master-DB NEVER-DELETE invariant preserved (`alpaca_submissions` is canonical; columns may be
ADDED). All existing `parity_mark` / `signal_pnl` behavior is **untouched** (this writes a
different table).

*Alternative considered:* a parallel `execution_order_ledger` table at the same
`(run_date, strategy_id, ticker)` key. Rejected for v0 — two columns co-located with the fill
they benchmark are simpler (no join), and `alpaca_submissions` already carries derived
reconciliation columns (`broker_status`/`filled_qty`/`filled_avg_price`), so this is consistent.

## 4. Capture mechanism — `finalize_execution_ledger(cur, closes, run_date)`

A new function in `src/execution/parity_mark.py` (co-located — reuses `_norm_ticker` /
`_safe_float`), a **sibling** to `finalize_parity_marks` (it does NOT touch the parity loop):

1. `SELECT id, strategy_id, ticker, direction, filled_avg_price, filled_qty, broker_status
    FROM alpaca_submissions WHERE run_date = %s`.
2. Per row, skip if:
   - `strategy_id` starts with `__` (sentinel close/orphan order — entry-only scope), or
   - `broker_status` is set and ∉ `('filled','partial')`, or
   - `filled_avg_price` is NULL / non-finite (no fill yet — leave ledger NULL, **deferrable**), or
   - `_norm_ticker(ticker)` not in `closes`, or
   - `direction` (lower-cased) ∉ `{'long','short'}`.
3. Compute:
   - `close = closes[_norm_ticker(ticker)]`
   - `direction_sign = +1 if direction.lower()=='long' else -1`
   - `signed_qty = direction_sign × _safe_float(filled_qty)` (skip if qty is NULL/non-finite)
   - `exec_ledger_usd = (close − filled_avg_price) × signed_qty`
4. `UPDATE alpaca_submissions SET official_close = %s, exec_ledger_usd = %s WHERE id = %s`.

Returns the number of rows updated. **Idempotent** — re-running recomputes identical values.
Early-returns `0` when `closes` is empty (mirrors `finalize_parity_marks`).

**Wiring:** called in the gated SP-6 4 PM block, immediately after `finalize_parity_marks(...)`,
with the same `closes` dict and `run_date`. Gate-off-inert (the block only runs under
`OPENCLAW_EOD_SIGNAL_REGISTER`).

### Sign convention (load-bearing — documented to prevent re-derivation drift)

`exec_ledger_usd > 0` ⟺ the order fill **beat** the close[T+1] benchmark:

- **long** order (`direction_sign = +1`): filled *below* close → `(close − fill) > 0`, `signed_qty > 0` → `+`.
- **short** order (`direction_sign = −1`): filled *above* close → `(close − fill) < 0`, `signed_qty < 0` → `+`.

This matches the §28 beat-close objective and is the single canonical place the signed-qty math
lives (the codebase has been bitten by divergent re-derivation — sharpe_cadence drift,
direction_sign parity).

## 5. Edge cases & invariants

- **No fill yet (timing) — `exec_ledger_usd = NULL`, deferrable.** The SP-6 `reconcile` step runs
  ~15:55 but Phase-A fills are *into the close* (~16:00), so at the 4 PM ledger pass
  `filled_avg_price` may not be reconciled yet → ledger NULL. Because the function is idempotent,
  any later same-`run_date` reconcile + re-call backfills it. This is a **Phase-A artifact**
  (into-close = the latest-possible fill, and the ledger is ≈0 there anyway); it **self-resolves
  in Phase B**, whose intraday fills reconcile well before the close. *Cross-day backfill is NOT
  built in v0* (the function processes only its own `run_date` + the closes passed to it); a
  reconcile-time backfill pass is a documented fast-follow if Phase-A ledger completeness ever
  matters.
- **Partial fill** → uses `filled_qty` (actual); `filled_avg_price` is its VWAP.
- **Fractional crypto** → NUMERIC throughout (migration 119 widened `alpaca_submissions.qty`);
  `_norm_ticker` matches BTC-USD (closes) vs BTC/USD (submission).
- **Ticker absent from `closes`** → skipped (no benchmark available).
- **Exit/orphan orders** → excluded via the `__`-prefix sentinel filter (entry-only scope).
- **Idempotent re-run** → identical `official_close` / `exec_ledger_usd`.
- **Gate-off inert** — only runs in the gated SP-6 4 PM block; with SP-6 OFF (current
  production) it never executes and the columns sit NULL. **B0 is byte-identical-when-off and
  safe to merge/deploy independently of Phase-A activation** — it starts populating the moment
  Phase A goes live.
- **Phase-A parity unchanged** — writes only `alpaca_submissions`; `execution_signals`,
  `signal_pnl`, `fill_price`, `mark_entry_price`, and the bracket re-anchor are all untouched.
- **Phase-A note (expected ~0 ledger):** in Phase A the fill is into the close, so
  `filled_avg_price ≈ official_close` and `exec_ledger_usd ≈ 0` by construction. B0's value in
  Phase A is proving the plumbing + starting the history; non-zero slippage appears only once
  B1's intraday scheduler replaces the naive into-close fill.

## 6. Testing (TDD)

`tests/test_sp6_b0_fill_capture.py` — live-DB rollback isolation (mirrors
`tests/test_sp6_parity_mark.py`'s `db_conn` fixture). No broker injection and no
`execution_signals` rows needed: insert `alpaca_submissions` rows + pass a `closes` dict.

1. long filled below close → `exec_ledger_usd > 0`, `official_close` set.
2. short filled above close → `exec_ledger_usd > 0`.
3. long filled above close → `exec_ledger_usd < 0` (sign sanity).
4. no fill (`filled_avg_price` NULL) → `official_close`/`exec_ledger_usd` NULL (deferrable).
5. partial fill → uses `filled_qty`.
6. fractional crypto (BTC/USD submission vs BTC-USD close) → captured, fractional qty preserved.
7. ticker absent from `closes` → row left NULL.
8. `__close_orphan__` (and any `__`-prefixed) row → excluded.
9. idempotent re-run → identical values.

Plus the existing `tests/test_sp6_parity_mark.py` suite must stay green (B0 touches a different
table + a separate function, so parity behavior is provably unaffected).

## 7. Rollout

Additive + gate-off-inert, so low-risk:

1. Build on a fast-follow branch off `feat/sp6-phase-a-eod-open-execution` (TDD,
   subagent-driven). Do **not** modify the reviewed Phase-A branch.
2. Apply migration 127 to the live DB (additive/idempotent — safe anytime, like 119/126). The
   live-DB tests require the columns to exist.
3. Merge into the Phase-A branch (or stack a PR) — inert until SP-6 activates.
4. After Phase A activates and runs a full cycle, spot-check: filled entry orders have
   `official_close` populated and `exec_ledger_usd ≈ 0` (into-close fill).

No service restart is required for the columns to exist; the capture path only executes under the
already-operator-gated SP-6 4 PM block.

## 8. Phase-B handoff

B0 delivers the measurement substrate at the order grain. **B1** (intraday scheduler) reads
`exec_ledger_usd` as its optimization objective and is the first consumer that drives it
non-zero; it owns exit-side TCA, the two-bracket split, and any per-strategy attribution /
per-symbol rollup it needs (aggregating up from the order grain B0 preserves). **B2** (Hawkes
alpha + §28 gate) reads the accumulated ledger as ground truth for alpha-randomization + replay
+ OOS before `w_hawkes` lifts above 0.
