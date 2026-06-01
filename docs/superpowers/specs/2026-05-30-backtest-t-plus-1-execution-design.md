# Backtest signal[t] → execute[t+1] Execution Model — Design

**Date:** 2026-05-30
**Author:** BotJohn (operator-directed)
**Status:** Approved design — ready for implementation plan

## Goal

Change the unified backtest so a signal generated on bar `t` fills on the **next bar's close** (`close[t+1]`) instead of the signal bar's close (`close[t]`). This removes the same-bar look-ahead in the entry fill, makes backtest metrics reflect a realistic next-day execution lag, and gives the backtest-coupled position-recommendations system a clean, consistent baseline to operate on.

Operator framing (verbatim): *"strategies take in EOD data and immediately submit orders in backtesting. Lets now revise this to signal[t] -> execute[t+1] being submission on the next day. This should be an elementary change in the backtesting process and should also alter metrics across strategies. Lets do this once ourself so that the position recs system can operate on a clean slate."*

## Background — the current fill

`src/backtest/unified_backtest.py::_per_bar_simulate` (the per-bar loop, lines ~542–598) is the single source of truth for per-trade fills. It is shared by:

- `run_backtest()` — the engine the **weekly refresh** and the **backtest-coupled recs** call (`src/execution/backtest_coupled_recs.py:57-58` → `ub.run_backtest(strategy_id, commit=False, ...)`).
- `universe_grid_cli` — the universe-recs grid.

Both inherit any change to `_per_bar_simulate` for free.

Today, for each signal on bar `t`:

1. `entry_price = sig.entry_price if set else close[t]` (line 553–554). The strategy's `entry_price` is the **signal-bar (t) close** in practice (see "Key finding" below), so the fill is effectively `close[t]` — a same-bar look-ahead: the decision and the fill use the same bar's close.
2. `stop_loss` / `target_1` are taken from the signal (or defaulted) relative to that `entry_price` (lines 555–558), then the regime/coupling override may re-anchor them (lines 559–564).
3. `simulate_trade(ticker_bars, current_date=t, ...)` walks exits from `index > t`, i.e. from `t+1` onward (line 237).
4. The trade is stamped `entry_date = t`, `entry_regime = regime_state(t)` (lines 588, 595).

### Key finding (changes the implementation surface)

**127 of ~140 strategy implementation files explicitly set `sig.entry_price`** to a signal-day price (RHS patterns: `current`, `price`, `current_price`, `cp`, `px`, `today_close`, all derived from `prices_to_date`, which ends at `t`). Therefore line 553 takes the `sig.entry_price` branch for almost every live trade, and the `else close[t]` fallback is rarely reached.

**Implication:** the t+1 fill must be applied at the **simulation boundary** — overriding whatever the strategy populated — *not* only in the fallback branch. Editing the fallback alone would silently miss nearly every live strategy.

## Scope

**In scope (the only file with behavior change):**
- `src/backtest/unified_backtest.py::_per_bar_simulate` — the entry-fill, bracket re-anchor, and the `entry_date` passed to `simulate_trade` and stamped on the trade record.

**Explicitly out of scope (verified to not use per-trade `close[t]` fills):**
- `quick_backtest.py` — weight-vectorized; already `.shift(1)`s weights to next period (line 294). No per-trade fill.
- `regime_blended_backtest.py`, `regime_performance_analyzer.py` — read precomputed `strategy_regime_backtests` rows; no fill loop.
- `intraday_regime_backtest.py` — separate intraday model on intraday bars.
- `options_backtest.py` — separate synthetic-options `simulate()`; candidate-only (no live option strategy). Its premium fill convention is independent and intentionally untouched.
- `auto_backtest.py` (in `src/strategies/`) — separate equity-only loop; not the unified path the recs system uses. (Out of scope for this change; if its fills should also move to t+1 that is a follow-up, but the recs/refresh clean-slate requirement is satisfied by unified alone.)

`crypto` (`S_btc_momentum`, `instrument_class='crypto'`, live) routes through `_per_bar_simulate` (only `'option'` diverges via `_simulate_for`), so it correctly inherits t+1 — appropriate, since crypto also executes next-bar in backtest terms.

## Design

### The fill change (inside `_per_bar_simulate`, per signal)

Replace the current entry-price/bracket construction with:

1. **Reference price** — what the strategy intended its brackets around:
   `ref = float(sig.entry_price) if (sig.entry_price and sig.entry_price > 0) else float(close[t])`.

2. **Locate t+1** — the first bar in `ticker_bars.index` strictly after `current_date`:
   `future = ticker_bars.index[ticker_bars.index > current_date]`. If empty (signal on the strategy's last available bar), **skip the trade** (`continue`) — it cannot be filled. Add a small counter for skipped-no-fill diagnostics in the returned dict (optional, non-breaking).

3. **Fill** — `fill_date = future[0]`; `entry_price = float(ticker_bars.loc[fill_date, 'close'])`. This **overrides** the strategy-supplied `entry_price` (the load-bearing consequence of the Key Finding).

4. **Brackets — re-anchor preserving pct shape** (mirrors the live executor's re-anchor; avoids inverted brackets on an overnight gap):
   - Derive the intended distances from `ref`:
     - long: `stop_pct = (ref - stop_ref) / ref`, `target_pct = (target_ref - ref) / ref`
     - short: `stop_pct = (stop_ref - ref) / ref`, `target_pct = (ref - target_ref) / ref`
     where `stop_ref`/`target_ref` are the signal's stop/target (or the existing defaults at lines 555–558, computed against `ref`).
   - Re-apply to the fill:
     - long: `stop_loss = entry_price * (1 - stop_pct)`, `target_1 = entry_price * (1 + target_pct)`
     - short: `stop_loss = entry_price * (1 + stop_pct)`, `target_1 = entry_price * (1 - target_pct)`

5. **Coupling/regime override unchanged** — the existing override block (lines 559–564) runs *after* step 4 and re-anchors stop/target to the new `entry_price` via `regime_param_override.apply_override`. No change there; it composes correctly because it keys off `entry_price`, which is now the t+1 fill.

6. **Sanity skips unchanged** — the wrong-side-of-entry guards (lines 568–571) now validate against the t+1 fill. Good.

7. **Exit walk from t+1's next bar** — call `simulate_trade(ticker_bars, fill_date, direction, entry_price, stop_loss, target_1, max_hold_days)`. Because `simulate_trade` walks `index > entry_date`, passing `fill_date` (t+1) makes exits start at **t+2** — no same-bar look-ahead on the fill bar either.

### Trade-record stamping

- `entry_date = fill_date` (t+1) — line 588 changes from `cur_d` to the fill date.
- `entry_price = close[t+1]` — line 589 (already the new value).
- `entry_regime = str(regime_state)` — line 595 **unchanged**: this is the **signal-day (t)** regime. `aggregate_per_regime` (line 411) buckets trades by this stored field, so per-regime metrics remain keyed to the decision day, which is what the regime-blended sizer and the Strategy Adjustments page expect. (No re-derivation from `entry_date` anywhere — verified.)
- `signal_stop` / `signal_target` — store the **re-anchored** values actually used (the post-step-4 / post-override stop/target), so `return_metrics`' `median_stop_pct` / `median_target_pct` (lines 842–848) reflect the brackets the simulation actually traded. This keeps the coupling's median-base derivation consistent with the new fills.

### What does NOT change

- `simulate_trade` itself (signature and exit logic) — we only change the `entry_date` argument we pass it.
- `aggregate_metrics`, `aggregate_per_regime`, the DB-write schema, `return_metrics` shape.
- The regime/coupling override helper (`regime_param_override`).
- Any strategy implementation file. Strategies keep emitting their signal-day `entry_price`; the simulator now treats it as the *reference* for bracket shape, not the fill.

## Live-vs-backtest conservatism (accepted asymmetry)

- **Live** fills ~same-day close (`close[t]`) — per the close-execution work (3pm proxy → execute-into-close).
- **Backtest** now fills `close[t+1]`.

The backtest therefore models a **strictly more conservative** fill than live receives (one extra overnight of gap/slippage exposure). This is intentional:

- A strategy/bracket that clears the auto-apply bar (ΔSharpe ≥ +0.10, ≥30 trades) under the harsher t+1 assumption should, if anything, perform **at least as well** live.
- The coupling decision is **backtest-to-backtest** (candidate bracket vs. baseline bracket, both on t+1), so the conservatism is a common level shift that cancels in the delta — it does not bias which recommendations get applied.

This asymmetry is documented, not a defect. If live execution ever moves to next-day fills, backtest and live converge with no further change.

## Testing

TDD, all against `_per_bar_simulate` / `run_backtest` (`commit=False` ephemeral, own-conn rollback — no DB pollution, panel rebuild already guarded by `if commit:`):

1. **Fill is next-bar close, overriding strategy entry_price.** Construct a tiny `bars_by_ticker` with known `close[t]` ≠ `close[t+1]`; a stub strategy that emits `entry_price = close[t]`. Assert the recorded trade `entry_price == close[t+1]` and `entry_date == t+1`.
2. **Bracket pct shape preserved.** Strategy emits stop/target at known pct from its `entry_price` (=ref). Assert recorded `signal_stop`/`signal_target` are the same pct distances re-applied to `close[t+1]` (within float tolerance), for both a long and a short.
3. **Exit walk starts at t+2.** Engineer a bar path where the target is touchable on t+1 but should be ignored (fill bar), and first reachable on t+2. Assert exit fires on/after t+2.
4. **Signal on last bar is skipped.** Signal on the final available bar → no trade recorded (no fill possible).
5. **Regime stamping = signal day.** Regime differs between t and t+1; assert `entry_regime` is the t (signal-day) regime, and the trade lands in that regime's bucket in `aggregate_per_regime`.
6. **Coupling override still composes.** With an injected `param_override`, assert brackets are re-anchored to the t+1 fill (override applied on top of the new entry), not to `close[t]`.
7. **Regression — no schema/shape drift.** A full `run_backtest(..., return_metrics=True, commit=False)` on one real live strategy returns the expected metrics dict keys incl. `median_stop_pct`/`median_target_pct`, and `aggregate_per_regime` rows for all canonical regimes.

## Rollout / clean-slate rebuild

1. **Let today's 12:00 UTC Saturday run finish on the OLD model.** `openclaw-weekend-saturday.timer` is armed (next: Sat 2026-05-30 12:00 UTC = 08:00 ET). It runs the current coupling on old metrics — the final old-model cycle. **No race:** the change is not landed until this run completes.
2. **Land the change** — implement in an isolated worktree (TDD), review, merge to `main` after the 12:00 UTC run completes.
3. **Full rebuild (operator-triggered):** run `unified_backtest` over all live strategies (`--all-live` driver) so every `strategy_backtest_runs` / `strategy_backtest_regimes` / `strategy_backtest_trades` row and the dashboard backtest panel is regenerated on t+1 metrics. Operator-triggered (not auto-fired by the merge), consistent with the safety posture on prod-affecting steps.
4. **Verify** a couple of strategies' Sharpe shifted (confirms the change took effect), dashboard panel refreshed. Next Saturday's coupling then operates on the clean t+1 baseline.

## Risks & mitigations

- **Missing the 127 explicit-`entry_price` strategies** → mitigated by overriding at the simulate boundary (step 3), with test #1 asserting the override regardless of strategy-supplied entry.
- **Inverted brackets on overnight gaps** → mitigated by pct-shape re-anchor (step 4) rather than carrying absolute levels across the gap; sanity guards (lines 568–571) remain as a backstop.
- **Sample-size shrink** (signals on last bar dropped, or first-day-of-data offset) → expected to be negligible (one bar per strategy lifetime per ticker); skipped-no-fill counter surfaces it if not.
- **Metrics shift surprising the coupling** → intended ("should also alter metrics across strategies"); the full rebuild (step 3) re-baselines everything before the next coupling, so no stale-vs-new mix.
- **DB pollution during tests** → all tests use `commit=False` (own-conn rollback; panel rebuild guarded by `if commit:`).
