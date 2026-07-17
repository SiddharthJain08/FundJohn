# True-MTM Daily-Returns Fix (Phase 1a) — Design

**Date:** 2026-07-01
**Status:** Design approved (operator: "Build Phase 1a (engine fix) now" from the feasibility spike)
**Type:** Backtest-engine methodology fix, **flag-gated default-OFF** (inert until a controlled re-backtest)

## Problem

`unified_backtest._portfolio_daily_returns` reconstructs a strategy's daily portfolio return series by
SMEARING each trade's total realized `pnl_pct` flat across its holding days (constant per day), then
equal-weight-averaging across concurrently-open trades. This strips intra-hold price volatility, so the
daily series is near-flat → `aggregate_metrics` produces **inflated, overlap-dependent Sharpe/Sortino/
Calmar** and **understated max-DD**. (Confirmed root cause — `docs/2026-07-01-metric-source-reconciliation-findings.md`.)

## Goal

Replace the smear with **true daily mark-to-market**: `simulate_trade` already walks each trade's daily
bars, so emit a real per-day return path and have `_portfolio_daily_returns` aggregate real marks. This
corrects both Sharpe AND max-DD while preserving the "Fix A" equal-weight-daily-curve behavior. Gated
behind `OPENCLAW_TRUE_MTM_MARKS` (default OFF → byte-identical to today) so deploying the code is inert
until the operator runs a controlled re-backtest with the flag ON.

## Non-goals (later phases)

- The re-backtest harness (Phase 1b), the probe (1c), the gated 189-strategy re-backtest (1d), and the
  live cascade — eligibility decoupling, Option-B mirror retirement, propagation (1e).
- The **DB-reconstruction gap** (deferred, operator-decided at 1e): `backtest_panel.py` (dashboard
  equity curve) and `strategy_returns.rebuild_daily_returns` → `strategy_daily_returns` →
  `strategy_similarity` (correlation matrix) rebuild trades from Postgres `strategy_backtest_trades`
  (no `daily_marks` column), so they keep the smear-fallback even with the flag ON. Not fixed here.
- Options / quick / regime_blended / intraday / auto backtest engines (don't share `_per_bar_simulate`).

## Design

### Component 1 — `simulate_trade` emits `daily_marks` (always; pure, no env read)
`src/backtest/unified_backtest.py:232-291`. During the existing per-bar walk, accumulate one
`(date, return)` per holding day into a list, and return it as a new `daily_marks` key at EVERY return
branch. Return convention (direction-signed, chained):
- `mark[0] = entry_price` (already the reanchored t+1 fill passed in).
- For holding day `i` (the loop's `dt`): `mark[i] = close` on non-exit days, and `mark[i] = exit_price`
  (`target_1` / `stop_loss` / last close) on the exit day.
- `daily_marks[i] = (dt, direction * (mark[i] / mark[i-1] - 1.0))`.
- Empty-window branch (`bars_future.empty`, holding_days==0) → `daily_marks = []`.
Computing marks is cheap (one append per bar) and keeps `simulate_trade` pure — the flag lives in the
caller, so scripts calling `simulate_trade` directly (bracket_stacking, universe_grid) are unaffected.

### Component 2 — `_per_bar_simulate` attaches marks only when the flag is ON
`src/backtest/unified_backtest.py`. Read the flag once at the TOP of `_per_bar_simulate` (function-level,
near the `_include_fill_bar` setup ~line 543, so tests can toggle it via `os.environ`):
```python
_true_mtm = os.environ.get('OPENCLAW_TRUE_MTM_MARKS') == '1'
```
Then in the trade-dict assembly (~690-703) attach:
```python
'daily_marks': exit_info.get('daily_marks', []) if _true_mtm else [],
```
Flag OFF → empty list → `_portfolio_daily_returns` smears (byte-identical today). Flag ON → real marks.
(`os` is already imported at line 41.)

### Component 3 — `_portfolio_daily_returns` prefers real marks
`src/backtest/unified_backtest.py:318-354`. Replace the smear block (337-346) with: if
`t.get('daily_marks')` is non-empty, add each `(date, ret)` to `daily_pnls[date]`; ELSE keep the
existing smear (options_backtest trades + hand-built test fixtures have no marks). The rest of
`aggregate_metrics` (equity curve, DD, return_pct, Sharpe, Sortino, Calmar) is unchanged — it just
operates on a realistic daily series. Real marks key by actual trading-day dates (not the current
`entry_date + Timedelta(days=i)` calendar-day synthetic), which also fixes weekend mis-alignment of
concurrently-open trades.

### Invariants
- **Flag OFF ⇒ byte-identical** to current output (no `daily_marks` attached → smear path).
- `len(daily_marks) == holding_days` for every non-degenerate trade (`[]` when holding_days==0).
- Longs: chained daily marks compound ≈ `pnl_pct`. Shorts: `compound(daily_marks) ≠ pnl_pct`
  (path-dependent MTM vs point-to-point) — EXPECTED, not a bug; do NOT assert equality for shorts.
- No schema change; `daily_marks` is transient (never in the `strategy_backtest_trades` INSERT tuple).
- No consumer breaks (no whole-dict/key-set assertions in the codebase; every reader accesses specific keys).

## Testing (`tests/test_true_mtm_marks.py`, unittest)
1. **Marks length/shape:** `simulate_trade` on a synthetic bar frame → `len(daily_marks)==holding_days`
   for target-exit, stop-exit, max_hold, end_of_data, and `==0` for the empty-window branch.
2. **Long compound ≈ pnl_pct:** a long max_hold trade → `abs(prod(1+r for _,r in daily_marks) - 1 - pnl_pct) < 1e-9`.
3. **Exit-day uses exit_price:** a target-exit long → the last mark's return reflects `target_1`, not the raw close.
4. **Realistic vol vs smear:** build a volatile synthetic trade set; `_portfolio_daily_returns` with
   marks yields a materially larger `std` (and `aggregate_metrics` a lower |Sharpe|) than the smear path
   on the same trades — the core assertion that the fix restores volatility.
5. **Flag OFF byte-identical:** with `_TRUE_MTM` False, `_per_bar_simulate`-produced trades carry
   `daily_marks == []` and `_portfolio_daily_returns` takes the smear path (assert identical output to
   pre-change for a fixed trade set).
6. **Smear-fallback:** a trade dict with no `daily_marks` key still smears (options/fixtures path).

## Rollout
1. Build + review (SDD) + deploy code (git push + on-disk). **Flag default OFF → inert**; the weekend
   cron and all current backtests keep today's behavior.
2. The controlled re-backtest (Phase 1d) runs each strategy with `OPENCLAW_TRUE_MTM_MARKS=1`.
3. Reversible: never flip the flag (or unset it) → smear behavior returns for any new backtest.
