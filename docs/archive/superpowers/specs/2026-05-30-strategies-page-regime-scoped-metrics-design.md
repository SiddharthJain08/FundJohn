# Strategies Page — Regime-Scoped Metrics + Regime Filter — Design

**Date:** 2026-05-30
**Author:** BotJohn (operator-directed)
**Status:** Approved design (Approach A) — ready for implementation plan

## Goal

On the dashboard **Strategies page → Active Stack**, the headline metric columns (Sharpe, Eff.Sharpe, Closed, Win %, ARR %, ADR %, ACT, Max-DD where shown) currently display **all-regime** backtest totals — even for strategies approved to trade only a **subset** of regimes (their *eligible regimes*). This misrepresents a strategy's expected behavior, because the regimes it will never trade are folded into its headline numbers.

Two changes:
1. **Default the headline metrics to an *eligible-regimes blend*** — aggregate only the regimes the strategy is approved/eligible for.
2. **Restore the per-regime filter** (the control still exists in the DOM but was hidden + stubbed when metrics went backtest-sourced) so the operator can scope the headline columns to **Eligible**, **All**, or any **single regime**.

Operator framing (verbatim): *"strategy metrics in the strategies page … currently show complete backtesting metrics for all regimes rather than selected regimes upon approval. There should also be a way to filter stats in each regime as there was before."*

## Background — current state (grounded)

- **Headline metrics** are read from `strategy_backtest_runs.total_*` (all-regimes-combined) via `ubtRunById` (`server.js:1202–1210`), surfaced by `buildStrategyRow` (`strategy_row.js`) as `sharpe / effective_sharpe / backtest_return_pct / backtest_max_dd_pct / closed_count / win_rate / arr_pct / adr_pct / act_days`.
- **Per-regime data** is already loaded into `unifiedBacktest[sid].regime_breakdown` from `strategy_backtest_regimes` (`server.js:1211–1242`), but the SELECT pulls only `trade_count, sharpe, max_dd_pct, return_pct, hit_rate` — **not** `avg_pnl_pct`, `avg_holding_days`, `oos_days_in_regime` (all three exist in the table per migration `093`).
- **Eligible regimes** are resolved per strategy as `_eligRaw` (registry-derived `_eligByStrategy[sid]`, else manifest `eligible_regimes`, else `null` = eligible-everywhere), and `_eligibleSet = new Set(_eligRaw || activeRegimes)` (`server.js:1348–1352`). Passed into `buildStrategyRow` as `eligRaw`.
- **The filter is dormant, not gone.** The DOM control exists (`server.js:4087–4095`, `.st-regime-filter` with buttons All / Low Vol / Transitioning / High Vol / Crisis + `#srf-hint`), the CSS exists (`server.js:3198–3208`), the state var `_stRegimeFilter` exists (`8267`), but `_renderActiveStack` hides it (`8542–8543`) and `_stSetRegimeFilter` is a no-op stub (`8269–8275`).
- **Reusable blend logic** already exists: `src/backtest/universe_grid_cli.py:blend_metrics` (day-frequency-weighted Sharpe with None-skip + renormalize; max-DD = max; trade-count-weighted win-rate; summed trades). We port its rules to JS — we do NOT call Python from the request path.
- **Per-regime `effective_sharpe` is not stored** (only the overall lives in `strategy_backtest_panel`). For any non-"All" scope it is **derived** as `blended_sharpe / sqrt(blended_avg_holding_days)` — the same formula `backtest_panel.effective_sharpe` uses (`sharpe / sqrt(cadence_days)`).

## Approach (A — server-side blend + client-side filter)

Compute a small `metrics_by_scope` object server-side per strategy, then let the client pick which scope the headline columns read. No schema change, no migration, no new endpoint.

### Architecture

```
strategy_backtest_regimes (per-regime rows, SELECT extended)
        │
        ▼
server.js: unifiedBacktest[sid].regime_breakdown  ── + avg_pnl_pct, avg_holding_days, oos_days_in_regime
        │
        ▼
blend_scope.js (NEW pure module)
   blendScope(regime_breakdown, regimeKeys) -> { sharpe, effective_sharpe, return_pct,
        max_dd_pct, closed_count, win_rate, arr_pct, adr_pct, act_days }
        │
        ▼
buildStrategyRow(...) attaches:
   metrics_by_scope = {
     ALL:        <existing total_* run metrics, unchanged>,
     ELIGIBLE:   blendScope(breakdown, eligibleRegimes),
     LOW_VOL:    blendScope(breakdown, ['LOW_VOL']),   // single-regime = that regime's raw row
     TRANSITIONING, HIGH_VOL, CRISIS: ...
   }
   default_scope = 'ELIGIBLE'   // 'ALL' when eligRaw is null (eligible-everywhere)
        │
        ▼
client _renderActiveStack:
   reads m = r.metrics_by_scope[_stRegimeFilter] ?? r.metrics_by_scope[r.default_scope]
   headline cells use m.*  (fallback to legacy r.* if a scope is absent)
   filter bar un-hidden; _stSetRegimeFilter(rg) sets _stRegimeFilter + re-renders
   sort keys read the active scope's values
```

### Components / units

1. **`src/channels/api/blend_scope.js` (NEW, pure, unit-testable)**
   - `blendScope(breakdown, regimeKeys) -> metrics | null`
     - `breakdown`: the per-regime map `{ LOW_VOL: {sharpe, max_dd, total_return_pct, trade_count, hit_rate, avg_pnl_pct, avg_holding_days, oos_days_in_regime}, ... }` (only regimes with rows present).
     - `regimeKeys`: array of regimes to include (e.g. eligible set, or a single regime).
     - Blend rules (mirror `blend_metrics`):
       - `closed_count` = Σ trade_count over included regimes.
       - `sharpe` = day-freq-weighted over included regimes with non-null sharpe, weight = `oos_days_in_regime` (fallback equal-weight if all weights 0); `null` if no contributor.
       - `max_dd_pct` = max `max_dd_pct` over included regimes (fraction→% consistent with the existing `r.backtest_max_dd_pct` units; see Edge cases).
       - `win_rate` = trade-count-weighted mean of `hit_rate` over included regimes with trades; `null` if 0 trades.
       - `arr_pct` = trade-count-weighted mean of `avg_pnl_pct` × 100 (ARR convention from `strategy_row.js:12`).
       - `act_days` = trade-count-weighted mean of `avg_holding_days`; `null` if 0 trades.
       - `adr_pct` = `arr_pct / max(1, act_days)` (matches `strategy_row.js:13`).
       - `effective_sharpe` = `sharpe / sqrt(act_days)` when both present and `act_days > 0`, else `null`.
       - `return_pct` = day-freq-weighted mean of `total_return_pct` over included regimes (informational; not a column today but cheap and consistent).
     - Returns `null` if `regimeKeys` is empty OR none of them have a breakdown row (caller falls back to ALL).
   - `module.exports = { blendScope }`.

2. **`server.js` — extend the per-regime SELECT** (`~1211–1220`) to also pull `br.avg_pnl_pct, br.avg_holding_days, br.oos_days_in_regime`, and add them to the `regime_breakdown[r.regime_state]` object (`~1235–1241`). Keep the existing `max_dd` fraction conversion; carry the new fields raw.

3. **`server.js` — build `metrics_by_scope`** at the row-builder call site (`~1358–1367`). Compute the ALL object from the existing `run`/`panel`/`bw` (reuse the exact field math already in `strategy_row.js` so ALL is byte-identical to today), then ELIGIBLE + 4 single-regime objects via `blendScope`. Pass `metricsByScope` + `defaultScope` into `buildStrategyRow`.

4. **`strategy_row.js` — emit the new fields.** Add `metrics_by_scope` and `default_scope` to the returned row. Keep all existing top-level fields (`sharpe`, `arr_pct`, …) exactly as-is so nothing else that reads them breaks (sorting, expansion panel, candidates table). ALL-scope values must equal those legacy top-level fields.

5. **Client `_renderActiveStack` (`~8536`)** — un-hide the filter (delete the `_rf.style.display='none'` block); on each render, set the active scope = `_stRegimeFilter` (default to the row's `default_scope` on first paint via a module init `let _stRegimeFilter = 'ELIGIBLE'`... see Edge cases for the All-vs-Eligible default). For each row, resolve `const m = (r.metrics_by_scope && r.metrics_by_scope[_stRegimeFilter]) || (r.metrics_by_scope && r.metrics_by_scope[r.default_scope]) || r;` and read headline cells from `m.sharpe / m.effective_sharpe / m.closed_count / m.win_rate / m.arr_pct / m.adr_pct / m.act_days`. The "By Regime" chip grid (`_regimeBreakdown`) is unchanged.

6. **Client filter bar** — add an **"Eligible"** button (default active) alongside All + the 4 regimes (`server.js:4087–4095`). Re-implement `_stSetRegimeFilter(rg)` (`8269`) to set `_stRegimeFilter = rg`, toggle `.active` on the buttons, update `#srf-hint` (e.g. "showing LOW_VOL backtest stats"), and re-render the active stack. Column-header `title`s gain a scope suffix when a non-ALL scope is active (the hook at `server.js:3208` "Inline tag in column headers when a regime filter is active" already anticipated this).

7. **Sorting** — `_applySort` reads `data-sort-key` off the row objects. Because headline values now live under `metrics_by_scope[scope]`, the renderer must either (a) project the active scope's metrics onto the row before sort, or (b) teach the sort to read the active scope. Chosen: **(a)** — in `_renderActiveStack`, build `enriched` rows with the active scope's metric fields spread onto a shallow copy (`{...r, sharpe: m.sharpe, win_rate: m.win_rate, ...}`) so existing sort keys keep working unchanged. This keeps sort logic untouched.

## Edge cases & decisions

- **`eligRaw === null` (eligible-everywhere).** `default_scope = 'ALL'` and the ELIGIBLE scope equals ALL. These strategies have no "subset," so all-regime IS the correct default; the filter still lets the operator drill into single regimes.
- **A single regime view = that regime's raw row** (one-element blend → identity). If the regime has `trade_count < 5`, its stored `sharpe` is already `null` (per `093` semantics) — the cell renders "—", which is correct and matches the chip grid.
- **Empty/absent breakdown** (strategy with a run row but no per-regime rows, or pre-backfill): `blendScope` returns `null` for non-ALL scopes; the client falls back to the row's `default_scope` then to legacy `r.*`. So worst case = today's behavior. **No regression for un-backfilled strategies.**
- **max_dd units.** Today `r.backtest_max_dd_pct` comes from `run.total_max_dd_pct` (a percent), while `regime_breakdown[rg].max_dd` is divided by 100 to a fraction at `server.js:1237`. `blendScope` must blend in ONE unit and emit the same unit the column formatter expects. Decision: blend on the raw `strategy_backtest_regimes.max_dd_pct` (percent, before the /100), and expose `max_dd_pct` as percent — matching the ALL column. The fraction-valued `max_dd` stays only for the chip-grid tooltip (unchanged).
- **Default scope choice (Eligible vs All on initial load).** Initial `_stRegimeFilter = 'ELIGIBLE'`. Per-row, when a strategy is eligible-everywhere its ELIGIBLE scope already equals ALL, so a global "ELIGIBLE" default is coherent for every row. The operator can click "All" to see true totals.
- **Inactive Stack / Research Candidates tables.** OUT of scope — they keep current behavior (candidates already show a per-regime BT-Sharpe grid via `_regimeBacktestSharpe`). Only the **Active Stack** headline columns + its filter change.
- **Expansion panel** (equity curve, similar strategies) — unchanged; reads `/api/strategies/:id/backtest-curve`, not the headline scope.

## Testing

- **`tests/test_blend_scope.js` (NEW, node):** unit-test `blendScope` directly (no server):
  1. Single-regime blend = identity (returns that regime's sharpe/win/arr; trade_count<5 → sharpe null).
  2. Two-regime eligible blend: day-freq-weighted sharpe matches hand-computed value; max_dd = max; win_rate trade-count-weighted; closed_count = sum; arr = trade-count-weighted avg_pnl×100.
  3. Empty regimeKeys → null. regimeKeys with no matching rows → null.
  4. effective_sharpe = sharpe/sqrt(act_days); null when act_days missing/0.
  5. A regime with null sharpe is skipped in the sharpe blend but still counts in win/closed.
- **Manual/smoke (no DB writes):** hit `/api/strategies` locally, assert a known subset-eligible live strategy's row has `metrics_by_scope.ELIGIBLE.sharpe !== metrics_by_scope.ALL.sharpe` and `default_scope === 'ELIGIBLE'`; an eligible-everywhere strategy has `default_scope === 'ALL'` and `ELIGIBLE` deep-equal `ALL`.
- **Regression:** existing `/api/strategies` consumers untouched — `buildStrategyRow` still emits every legacy top-level field; ALL-scope equals legacy values (assert `metrics_by_scope.ALL.sharpe === row.sharpe`).

## Rollout

Dashboard-only change in `johnbot`'s `src/channels/api/server.js` (+ `strategy_row.js`, new `blend_scope.js`). No migration, no gate. Ship = commit → operator `git pull` on VPS → restart johnbot (`systemctl --user restart johnbot`). Purely additive to the payload; if `metrics_by_scope` is absent (old payload, cached client) the client falls back to legacy `r.*`, so a stale tab degrades gracefully. Restart is an operator step (prod service), consistent with the safety posture.

## Out of scope

- t+1 backtest branch (separate, parked).
- Inactive Stack / Research Candidates metric scoping.
- Any change to how eligible regimes are *set* (the regime-cell toggle + `/api/regime-eligibility` are unchanged).
- Recomputing/persisting per-regime effective_sharpe in the DB (derived on the fly instead).
