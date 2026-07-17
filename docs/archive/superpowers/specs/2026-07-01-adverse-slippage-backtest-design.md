# Adverse Slippage in Backtest (Phase 1a-slip) — Design

**Date:** 2026-07-01
**Status:** Design approved (operator: pause & restart with slippage; equity/etp 10bps one-way)
**Type:** Backtest-engine cost model — apply an always-adverse per-fill slippage, flag-gated

## Problem

`unified_backtest` has a per-class cost table `INSTRUMENT_COST_BPS` but it is **record-only** (line 16:
"no slippage / commission model"; logged at line 826, never applied to the sim). So backtest Sharpe
reflects *paper* edge — it assumes frictionless fills. Baking in a conservative, always-unfavourable
slippage makes the resulting Sharpe indicative of *harvestable* edge. We do it now so the in-flight
true-MTM re-backtest (paused) runs once with both corrections.

## Goal

Apply a one-way adverse execution cost `s` (bps) to the effective entry AND exit fills inside
`simulate_trade`, direction-aware, so `pnl_pct` and the true-MTM `daily_marks` both reflect ~2·s of
round-trip drag. Flag-gated `OPENCLAW_BACKTEST_SLIPPAGE=1` (default OFF → byte-identical), set alongside
`OPENCLAW_TRUE_MTM_MARKS` for the re-backtest.

## Non-goals
- Liquidity/volatility/ADV-scaled slippage (a flat per-class bps is the right conservative worst-case; scaled is a future refinement).
- Options / quick / regime engines (don't share `simulate_trade`; `options_backtest` has its own pricing).

## Design

### Magnitude — recalibrate the existing per-class knob (one knob, no double-count)
`INSTRUMENT_COST_BPS` reinterpreted as a **one-way adverse execution cost** (subsumes half-spread +
slippage + commission), applied at entry AND exit → round-trip ≈ 2×:
```python
INSTRUMENT_COST_BPS = {"equity": 10.0, "etp": 10.0, "option": 5.0, "crypto": 25.0}
```
(equity/etp 1.0→10.0 per operator; option 5.0 / crypto 25.0 already the operator's one-way numbers.)

### Mechanics — adverse fills in `simulate_trade`
Add param `slippage_bps: float = 0.0`. Let `s = slippage_bps / 10000.0`, `dir = direction` (+1 long / −1 short):
- **Entry fill** (transact in the position's direction — pay up to buy a long, sell down a short):
  `entry_fill = entry_price * (1 + dir * s)`.
- **Exit fill** (opposite transaction — always give back): for each exit branch with level `L`
  (`target_1` / `stop_loss` / last close), `exit_fill = L * (1 - dir * s)`.
- **pnl** off fills (unified): `pnl = dir * (exit_fill - entry_fill) / entry_fill`.
  (Verifies: long → (exit_fill−entry_fill)/entry_fill; short → (entry_fill−exit_fill)/entry_fill.)
- **daily_marks** off fills: `prev_mark` starts at `entry_fill`; non-exit days mark to `close`; the exit
  day marks to `exit_fill`. So the entry-day return uses the adverse basis and the exit-day return the
  adverse proceeds — the ~2·s drag lands on the boundary marks, none on the intermediate MTM closes
  (correct — those aren't transactions). `compound(daily_marks) == pnl` for longs still holds (telescopes
  through the fills).
- **Stored fields:** the returned `exit_price` = `exit_fill` (the honest transacted price, inclusive of
  slippage). The signal levels remain in `signal_stop`/`signal_target` (unchanged, set by
  `_per_bar_simulate`). The trade-dict key-set is UNCHANGED (no new per-trade field → flag-OFF truly
  byte-identical); slippage is recorded at the run level via the existing `_log` at line 826, extended to
  `cost_model_bps=X slippage_applied=<bool>`.
- **Last-bar/max_hold handling:** fold the max_hold/end-of-data exit INTO the loop's final iteration (it
  is the exit bar) so its mark uses `exit_fill` — cleaner than the current post-loop block and avoids a
  double-marked last bar. Byte-identical when `s=0`.
- **Always unfavourable:** a winning trade's win shrinks; a losing trade's loss grows; a stop fills BELOW
  the stop (long) / ABOVE (short) — models stop/gap slippage. Both directions reduce pnl.

### Threading + gating
- `run_backtest` computes `slippage_bps = resolve_cost_model_bps(instrument_class)` **iff**
  `os.environ.get('OPENCLAW_BACKTEST_SLIPPAGE') == '1'`, else `0.0`; passes it into
  `_per_bar_simulate(..., slippage_bps=slippage_bps)`.
- `_per_bar_simulate` gains `slippage_bps: float = 0.0` and forwards it to every `simulate_trade` call.
- `simulate_trade(slippage_bps=0.0)`: `s == 0` ⇒ `entry_fill == entry_price`, `exit_fill == L` ⇒
  **byte-identical** to today. Flag OFF ⇒ `slippage_bps` stays 0 everywhere.

### Invariants
- Flag OFF ⇒ byte-identical (s=0 collapses fills to true prices).
- Slippage never *improves* a trade: for s>0, `pnl_with ≤ pnl_without` for every branch and both directions.
- `len(daily_marks) == holding_days` unchanged; longs `compound(marks)==pnl` (off fills); shorts not asserted.
- No schema change (`entry_price`/`exit_price` columns already exist; `slippage_bps` is transient, not in the trade INSERT tuple).

## Testing (`tests/test_adverse_slippage.py`, unittest, synthetic bars)
1. **s=0 byte-identical:** `simulate_trade(..., slippage_bps=0.0)` equals the no-arg call (pnl, marks, prices).
2. **Long adverse:** s>0 → `entry_price` (returned fill) > input entry; `exit_price` < exit level;
   `pnl` strictly < the s=0 pnl; `compound(marks) ≈ pnl`.
3. **Short adverse:** s>0 → entry fill < input entry; exit fill > level; `pnl` strictly < s=0 pnl.
4. **~2·s drag:** a long with exit level == entry (zero gross) → `pnl ≈ -2s` (within rounding).
5. **Stop fills worse:** long stop exit → returned `exit_price` < `stop_loss`.
6. **Gating (resolve):** with `OPENCLAW_BACKTEST_SLIPPAGE` unset, `run_backtest`'s effective slippage is 0
   (test the resolve expression / a `_per_bar_simulate` call passes 0 → byte-identical); with it '1',
   equity resolves 10.0.

## Rollout
Build + review + commit + push. Then **restart the re-backtest** with BOTH flags
(`OPENCLAW_TRUE_MTM_MARKS=1 OPENCLAW_BACKTEST_SLIPPAGE=1`) and a fresh `start_ts` (delete
`/var/log/openclaw/rebacktest/state.json`) so all 155 redo with true-MTM + adverse slippage. Re-arm the
completion notifier. Live trading unaffected until Phase 1e.
