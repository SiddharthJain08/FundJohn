# Universe-shrink metrics use a different Sharpe methodology than the backtest of record — findings (2026-11-24)

**Trigger (operator):** "S_beta_spy turned negative in LOW_VOL suddenly, affecting activation"; "strategies with positive Sharpe in every regime show ~1.0x at the end of the backtest chart (S_ast_earnings_announcement_premium, S_rachev_ratio_etf_scenario_opt)".

## 1. What the operator saw, and where it comes from
- `/api/strategies` (Strategies page rows, per-regime cells, scope blends) OVERLAYS `universe_shrink_metrics` chosen rows on the primary run (server.js "Chosen-universe overlay"). `activation_assigner._fetch_backtest_rows` and `strategy_weights` tier 0 do the same. The equity chart (`strategy_backtest_panel.equity_curve`) does NOT — it is built from true daily marks.
- S_beta_spy (run `79ce96de`, 2648 trades, universe {SPY}): run rows LOW_VOL **+0.94**, TOTAL 0.66 / +271 %. Shrink rows for the SAME run and the SAME 926/2648 trades: LOW_VOL **−0.262**, CRISIS 12.79, TOTAL 1.106 / +511 %. `strategy_weights_by_regime.bt_sharpe` LOW_VOL flipped 0.9378 → −0.262 on **2026-11-05** (the canonical-sequence universe-shrink run, candidate set `shrink-1-shrink-20261007`) and has stayed there.
- S_ast_earnings_announcement_premium (run `7f46cb63`): run 0.23 / +19.9 %, LOW_VOL −0.10; shrink 3.01 / **+1153 %**, LOW_VOL +3.30. Chart ends 1.24x (marks) — the "positive Sharpe everywhere but flat chart" contradiction.
- S_rachev_ratio_etf_scenario_opt (run `34ebf459`): run 0.50 / +36 %; shrink 3.00 / +87 %, LOW_VOL 2.70 vs 0.55.

## 2. Root cause (reproduced)
`scripts/run_universe_shrink.py` reloads the primary run's trades from `strategy_backtest_trades_primary` with only (ticker, entry_date, pnl_pct, holding_days, entry_regime) and calls `unified_backtest.aggregate_per_regime` / `aggregate_metrics`. Stored trades carry no `daily_marks`, so `_portfolio_daily_returns` falls back to the LEGACY SMEAR (pnl spread evenly over `holding_days` calendar days). The backtest of record computes the same aggregates on `daily_marks` (true mark-to-market path, `OPENCLAW_TRUE_MTM_MARKS`), and `backtest_panel.prepare_trades_for_curve(trades, closes_for)` rebuilds those marks from prices for the chart.

Re-computation on 2026-11-24 (scratch `sharpe_repro.py` / `sharpe_repro2.py`):

| strategy / scope | stored run | re-agg SMEAR (what shrink wrote) | re-agg TRUE MARKS |
|---|---|---|---|
| S_ast_earnings TOTAL | 0.23 / +19.9 % | 2.75 / +1153 % | 0.19 / +24 % |
| S_ast_earnings LOW_VOL | −0.10 / −18.6 % | 3.09 / +182 % | −0.11 / −13 % |
| S_beta_spy LOW_VOL | +0.94 / +48 % | −0.26 (macro rf) / +7 % | (n/a — same mechanism) |
| S_beta_spy TOTAL | 0.66 / +271 % | 1.20 / +511 % | — |

The smear path is especially wrong for short-hold strategies (median hold 2 d for S_ast_earnings: 712 one-day trades averaging −1.8 % get diluted/smoothed against multi-day winners) and even flips the sign of the SPY sleeve's LOW_VOL cell. rf source `macro` vs `const` moves the SPY cell between −0.26 and −0.92 — a second-order inconsistency (the shrink pass ran with `macro`, the dashboard labels are rf-agnostic).

## 3. Blast radius (measured 2026-11-24)
- 1634 chosen shrink cells across 173 strategies; 1136 differ from the run row by > 0.5 Sharpe; 350 sign flips.
- Isolating the methodology (cells whose shrink trade_count == run trade_count, i.e. no real universe change): 537 cells / 106 strategies — 479 differ by > 0.1, 245 by > 1.0, 102 sign flips, mean ratio 1.59× (inflation).
- Live eligibility (`strategy_regime_params`, approved strategies, 428 cells, 386 fed by shrink rows): **58 cells ON whose run-of-record Sharpe is below the bench − 0.10** (e.g. S_tsmom_cross_asset_vol_scaled LOW_VOL run −0.20 vs shrink 1.89; S_ast_earnings LOW_VOL −0.10 vs 3.30; S_price_earnings_momentum_drift HIGH_VOL −0.51 vs 1.91), **23 cells OFF whose run Sharpe ≥ bench with n ≥ 100**. The 2026-11-05 activation pass flipped 116 cells (95 ON) on these numbers.
- Weights: every non-sleeve strategy's `bt_sharpe` (→ `effective_sharpe` → `daily_weight`) comes from the smear numbers, clamped at `bt_sharpe_plausibility_cap` = 5.0 (S_beta_spy CRISIS 12.79 → 5.0; the sleeve's own weight := S_m so it is unaffected). The activation BENCH vector is NOT affected (load_bench_sharpe reads `strategy_backtest_regimes`: LOW_VOL 0.9378) — so candidates were judged inflated against an honest bar ⇒ over-activation.
- Dashboard: Strategies rows/cells/scopes show the smear numbers; the chart shows marks. Portfolio/Activation panels unaffected.

## 4. Recommended fix (not built — operator decision)
1. `universe_shrink.shrink_and_select` (and `universe_grid_cli._simulate_grid` if it ever aggregates stored trades) must aggregate on true marks: load the run's trades WITH exit_date, run `backtest_panel.prepare_trades_for_curve(trades, closes_for)` (filtered price read, ~0.3 GB per strategy as the panel does), then aggregate. Parity guard: refuse to write chosen rows when the FULL tier's TOTAL Sharpe differs from `strategy_backtest_runs.total_sharpe` by > 0.05 (same trades ⇒ must match).
2. Re-run `run_universe_shrink --force --adopt --reassign` for live+candidate strategies (173; heavy — weekend/after-23:30Z window, serialized, nice 19), then `strategy_weights --rebuild`. Expect ≈ 58 cells to deactivate and ≈ 23 to activate; the book's weights change materially.
3. Until then, an interim read-side rule: when a chosen shrink row has the same trade_count as the run row, consumers (activation, weights, dashboard) should take the run row. This alone fixes S_beta_spy and the 537 "no real shrink" cells.
