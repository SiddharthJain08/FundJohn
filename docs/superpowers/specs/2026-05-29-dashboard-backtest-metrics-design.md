# Strategy Dashboard — Backtest-Sourced Metrics (Design)

- **Date:** 2026-05-29
- **Status:** Approved (design); pending spec review → implementation plan
- **Owner:** BotJohn
- **Surface:** user-facing dashboard — `src/channels/api/server.js` (Express, :3000 → nginx :80)

## 1. Motivation

Corroboration (cross-sector + confirmation framework) is now the primary
quality process for strategies, and individual strategies may or may not
reach their stops in live trading. Live per-strategy performance is therefore
a noisy, low-sample signal that the dashboard currently over-emphasizes
(open positions, live win-rate, live OUE). We are shifting **every** strategy
metric on the dashboard to be **backtest-derived**, leaving only the two
metrics that are inherently live: **last signal** and **status**.

This is independent of, and does not change, the live trade pipeline or the
`#trade-reports` OUE digest (which remains a live closed-position report).

## 2. Goals / Non-Goals

**Goals**
- Source all strategy metrics from backtest data; keep `last_signal_date` and
  `status` as the only live-derived fields.
- Remove "open positions" / "opened" everywhere (no longer relevant).
- Add **Sharpe** and **effective Sharpe** (`Sharpe / sqrt(cadence)`).
- Add a **backtest-derived OUE** (Over/Under/Expected), separate from the live
  `#trade-reports` OUE digest.
- Advanced metrics: **full backtest simulation vs SP500 with regime overlay**,
  plus a **positions-closed-per-regime bar** (opened removed).
- Keep all existing metric *types*, recomputed from backtest.

**Non-Goals**
- No change to the live execution pipeline, sizer, or the `#trade-reports`
  digest.
- No change to the separate Control-Room dashboard (`:7870`).
- Not introducing a portfolio-level backtest curve (per-strategy only).

## 3. Decisions (resolved with operator)

1. **Backtest OUE = GBM σ (reuse live math).** Per backtest trade,
   `sigma_delta = pnl_pct / (hv21 · sqrt(holding_days / 252))` with
   `hv21` computed from `prices.parquet` at `entry_date` and `ev_gbm = 0`
   (zero-drift baseline). Classify O/U/E at `±sigma_gate` (`pipeline_config.sigma_gate`,
   currently `2.0`). Reuses `src/execution/oue_classifier.py:classify()`.
   Invariant: `O + U + E = total backtest trades`.
2. **Effective-Sharpe cadence = backtest `avg_holding_days`** (from
   `strategy_backtest_runs.avg_holding_days`). `effective_sharpe = total_sharpe / sqrt(max(1, avg_holding_days))`.
3. **3 live strategies missing trade-level backtest data**
   (`S_HV16_gex_regime`, `S_idiosyncratic_vol_puzzle`, `S_price_path_convexity`):
   **run `unified_backtest` for them first** so all 51 live strategies render fully.
4. **Architecture = precompute a per-strategy panel table** (Approach A).
   The new OUE and SP500 overlay need `hv21` / `^GSPC` from `prices.parquet`
   (pandas/Python), which node SQL can't read; computing per-request over
   4.3M trades is wasteful. Compute once in Python, store, read fast from node.

## 4. Current state (to be replaced)

`GET /api/strategies` (`server.js:1154`) currently joins a live `strategy_stats`
view + `signal_pnl` + `execution_signals` for most metrics, with backtest
fields as a secondary source. Live-derived today (to be removed/replaced):
`open_count`, `closed_count`, `wins/losses/win_rate`, `avg_realized_pct`,
`avg_unrealized_pct`, `best/worst_trade_pct`, `avg_days_held`,
`live_regime_breakdown`, `live_sharpe/live_days/live_return`,
`d1_overperf/underperf/expected` (live OUE), `oue_multipliers_by_regime`.
Backtest-derived already: `backtest_sharpe/return/max_dd/trade_count`,
`backtest_regime_breakdown` (from `strategy_backtest_runs/regimes`).

Frontend: active-stack table `_renderActiveStack()` (`server.js:~8729`) with an
**Open** column, **#O/U/E** (live), per-regime grid; expansion panel has a live
**ARR-curve** chart and an **opened/closed position-flow** chart.

## 5. Architecture

```
unified_backtest.py  ──writes──▶  strategy_backtest_runs / _regimes / _trades   (existing)
                                            │
backtest_panel.py (NEW)  ──reads trades + prices.parquet──▶  strategy_backtest_panel (NEW, 1 row/strategy)
        │  • GBM-σ OUE (overall + per regime)
        │  • effective_sharpe
        │  • equity curve {date, strat_equity, spx_equity, regime} (weekly-downsampled)
        ▼
server.js  /api/strategies            ──reads runs/_regimes/_panel + last_signal/status (live)
           /api/strategies/:id/backtest-curve  ──reads _panel.equity_curve
        ▼
Frontend: backtest table + advanced charts (equity-vs-SP500 regime overlay, closed-per-regime bar)
```

Reused as-is (already backtest): `strategy_backtest_runs`
(total_sharpe, total_return_pct, total_max_dd_pct, total_trades, total_hit_rate,
avg_holding_days, primary_window), `strategy_backtest_regimes`
(regime_state, trade_count, sharpe, return_pct, hit_rate, avg_pnl_pct,
avg_holding_days) — `trade_count` per regime *is* "positions closed per regime".

## 6. Data layer — new table `strategy_backtest_panel`

Migration adds one table (additive; no drops — honors the never-delete invariant).
One row per strategy, upserted by the builder; `computed_at` for freshness.

```sql
CREATE TABLE IF NOT EXISTS strategy_backtest_panel (
    strategy_id        TEXT PRIMARY KEY,
    run_id             UUID,                 -- the primary_window run this was built from
    effective_sharpe   DOUBLE PRECISION,     -- total_sharpe / sqrt(max(1, avg_holding_days))
    cadence_days       DOUBLE PRECISION,     -- avg_holding_days used in the divisor
    oue_over           INTEGER,
    oue_under          INTEGER,
    oue_expected       INTEGER,
    oue_by_regime      JSONB,                -- {regime: {over, under, expected}}
    oue_sigma_gate     DOUBLE PRECISION,     -- gate used (audit)
    equity_curve       JSONB,                -- [{date, strat_equity, spx_equity, regime}, ...] weekly
    n_trades           INTEGER,              -- trades the panel was computed over
    computed_at        TIMESTAMPTZ DEFAULT NOW()
);
```

## 7. Backtest-panel builder — `src/backtest/backtest_panel.py`

Pure-ish module; `build_panel(strategy_id) -> dict` + `--rebuild [--strategy-id X]` CLI.
Inputs: latest `primary_window=TRUE` run's `strategy_backtest_trades`
(`entry_date, exit_date, exit_reason, pnl_pct, holding_days, entry_regime, ticker`),
`prices.parquet` (per-ticker close → `hv21`; `^GSPC` for benchmark),
`historical_regimes.parquet` (date→regime).

- **OUE (GBM σ):** for each trade, `hv21` = annualized stdev of 21 daily log-returns
  of `ticker` ending at `entry_date` (from prices); reuse
  `oue_classifier.classify(pnl_pct, holding_days, ev_gbm=0.0, hv21, sigma_gate)`.
  Tally O/U/E overall and grouped by `entry_regime`. Trades with no
  computable `hv21` (insufficient price history) → `expected` (matches live
  classifier's missing-EV fallback), keeping the `O+U+E = n_trades` invariant.
- **Equity curve:** order closed trades by `exit_date`; cumulative
  `strat_equity` = ∏(1 + pnl_pct) booked at each `exit_date` (step series,
  forward-filled to trading days). `spx_equity` = `^GSPC` cumulative return over
  the same `[start,end]` window, normalized to the same starting value (1.0).
  Each point tagged with that day's `regime` from `historical_regimes.parquet`.
  **Downsample to weekly** (≤ ~520 points) for a compact JSON payload.
- **effective_sharpe** = `total_sharpe / sqrt(max(1, avg_holding_days))`.
- **Freshness hook:** invoke after `unified_backtest` persists a run; also a
  standalone `--rebuild` for backfilling all strategies.
- **Missing data:** strategies with no `primary_window` trades → no panel row;
  the API/frontend renders "no backtest data yet" placeholders. (After step 1
  of the build sequence, all 51 live strategies have trades.)

## 8. API changes (`server.js`)

`GET /api/strategies` — per strategy:
- **Core (backtest):** `sharpe` ← `strategy_backtest_runs.total_sharpe`;
  `effective_sharpe`, `oue_over/under/expected`, `oue_by_regime` ←
  `strategy_backtest_panel`; `closed_count` ← `total_trades`;
  `win_rate` ← `total_hit_rate`; `best`/`worst` ← `strategy_backtest_trades`
  min/max `pnl_pct`; per-regime grid ← `strategy_backtest_regimes`. Explicit
  ARR/ADR/ACT mapping (kept as metric *types*, now backtest):
  **ACT** (avg closing time) = `avg_holding_days`; **ARR** (avg return rate per
  trade) = mean trade `pnl_pct` (overall via `AVG(pnl_pct)` on
  `strategy_backtest_trades`; per-regime via `strategy_backtest_regimes.avg_pnl_pct`);
  **ADR** (avg daily return) = `ARR / max(1, ACT)`.
- **Live (only):** `last_signal_date` (`MAX(signal_date)` from `execution_signals`),
  `status` fields (`state`, `is_stale`, `regime_active`, `current_regime`).
- **Removed:** `open_count`, `avg_unrealized_pct`, `live_sharpe/live_days/live_return`,
  `live_regime_breakdown`, `d1_*` (live OUE), `oue_multipliers_by_regime`.

New `GET /api/strategies/:id/backtest-curve` → `strategy_backtest_panel.equity_curve`
JSON for the chart (404/empty-state when no panel row).

## 9. Frontend changes (`server.js` templates)

- **Active-stack table** (`_renderActiveStack`): remove **Open** column; add
  **Sharpe** and **Eff.Sharpe** columns; **Closed/Win%/ARR/ADR/ACT** → backtest
  values; **By Regime** grid stays (already backtest); **#O/U/E** → backtest OUE;
  **Last Signal** + **Status** stay live. Update sort keys accordingly.
- **Expansion → Advanced metrics:** replace the live ARR-curve chart and the
  opened/closed position-flow chart with:
  1. **Backtest equity vs SP500** line chart (two series) with **regime-band
     background shading** (from the curve's per-point `regime`).
  2. **Positions-closed-per-regime bar** (from `strategy_backtest_regimes.trade_count`).
- Per-regime live stats grid in the expansion → backtest per-regime stats.

## 10. Removed / kept

| Metric | After |
|---|---|
| Sharpe, Eff.Sharpe, Closed, Win%, ARR, ADR, ACT, best/worst, by-regime, OUE | Backtest |
| Last signal, Status (state/stale/regime-active) | Live (only these) |
| Open positions / opened, live P&L, live OUE, OUE multiplier display | Removed |

## 11. Edge cases

- Strategy with a panel but zero trades in a regime → that regime shows 0
  closes / no OUE; bar omits or zeroes it.
- `hv21` uncomputable (thin/short price history for a ticker) → trade counted
  `expected` (documented fallback).
- Curve window with sparse trades → step/forward-fill; SP500 normalized over the
  strategy's own `[start_date, end_date]`.
- Stale panel (backtest re-ran but builder didn't) → surface `computed_at`;
  builder runs in the same flow to avoid drift.

## 12. Testing

- **`backtest_panel` pure tests:** GBM-σ OUE classification on synthetic trades
  (over/under/expected boundaries; missing-hv21 → expected; invariant
  `O+U+E=n`); equity-curve compounding + SP500 normalization to common start;
  `effective_sharpe = sharpe/sqrt(cadence)`; regime tagging.
- **API contract test:** `/api/strategies` payload includes `sharpe`,
  `effective_sharpe`, backtest OUE; carries only `last_signal_date`/`status`
  as live; asserts `open_count` is absent.
- **Builder smoke** against a covered live strategy: panel row written, curve
  non-empty, OUE sums to trade count.

## 13. Build sequence (for the implementation plan)

1. Run `unified_backtest` for the 3 missing live strategies (populate `_trades`).
2. Migration: `strategy_backtest_panel`.
3. `backtest_panel.py` builder + unit tests (TDD).
4. Build panels for all 51 live strategies; verify coverage + invariants.
5. `/api/strategies` rewrite (backtest sources; live = last_signal + status only)
   + new `/backtest-curve` endpoint + API tests.
6. Frontend rewrite (table columns; equity-vs-SP500 regime-overlay chart;
   closed-per-regime bar; remove opened/open).
7. Verify on the live dashboard; restart `johnbot` if needed.

## 14. Risks / rollback

- **Dashboard-only change**; no live-trading behavior affected. Rollback =
  revert the `server.js` block and drop nothing (panel table is additive).
- Curve JSON size: weekly downsampling keeps payloads small (~KBs/strategy).
- Builder freshness: hook into the backtest flow + a manual `--rebuild` guard so
  panels don't silently lag `strategy_backtest_runs`.
