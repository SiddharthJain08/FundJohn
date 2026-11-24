# Universe-shrink Sharpe fix — interim read-side rule, then marks-based shrink — design

**Date:** 2026-11-24 · **Operator ruling (21:09Z):** "go with both, interim rule first then the full fix". Findings: `docs/superpowers/specs/2026-11-24-shrink-sharpe-methodology-defect.md`.

## 1. Intent
Every consumer of per-regime backtest Sharpe (activation assigner, weights builder, Strategies page) must see numbers computed with the backtest-of-record methodology (true daily marks). Today `universe_shrink_metrics` is computed on the legacy smear and is preferred by all three.

## 2. Approaches
- **Chosen:** (A) a DB view that substitutes run-of-record rows wherever a chosen shrink row covers the identical trade set (interim, immediate); then (B) make the shrink pass aggregate on true marks with a parity guard, and re-run it so ladder tiers are also honest.
- Rejected: patch each consumer's query with its own CASE expression (three copies drift); or drop the shrink preference entirely (loses the ladder-tier metrics the universe campaign depends on).

## 3. Part A — interim rule (`universe_shrink_metrics_effective`)
Migration `src/database/migrations/170_universe_shrink_metrics_effective.sql`:
```sql
CREATE OR REPLACE VIEW universe_shrink_metrics_effective AS
SELECT m.id, m.strategy_id, m.run_id, m.tier, m.regime_state, m.chosen, m.candidate_set_id, m.rec_id, m.computed_at,
       m.trade_count,
       CASE WHEN br.trade_count = m.trade_count THEN br.sharpe            ELSE m.sharpe            END AS sharpe,
       CASE WHEN br.trade_count = m.trade_count THEN br.max_dd_pct        ELSE m.max_dd_pct        END AS max_dd_pct,
       CASE WHEN br.trade_count = m.trade_count THEN br.hit_rate          ELSE m.win_rate          END AS win_rate,
       CASE WHEN br.trade_count = m.trade_count THEN br.sortino           ELSE m.sortino           END AS sortino,
       CASE WHEN br.trade_count = m.trade_count THEN br.calmar            ELSE m.calmar            END AS calmar,
       CASE WHEN br.trade_count = m.trade_count THEN br.avg_holding_days  ELSE m.mean_holding_days END AS mean_holding_days,
       CASE WHEN br.trade_count = m.trade_count THEN br.return_pct        ELSE m.return_pct        END AS return_pct,
       (br.trade_count = m.trade_count) IS TRUE AS from_run_row
  FROM universe_shrink_metrics m
  LEFT JOIN strategy_backtest_regimes br ON br.run_id = m.run_id AND br.regime_state = m.regime_state
 WHERE m.regime_state <> 'TOTAL'
UNION ALL
SELECT m.id, m.strategy_id, m.run_id, m.tier, m.regime_state, m.chosen, m.candidate_set_id, m.rec_id, m.computed_at,
       m.trade_count,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_sharpe       ELSE m.sharpe            END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_max_dd_pct   ELSE m.max_dd_pct        END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_hit_rate     ELSE m.win_rate          END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_sortino      ELSE m.sortino           END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_calmar       ELSE m.calmar            END,
       CASE WHEN r.total_trades = m.trade_count THEN r.avg_holding_days   ELSE m.mean_holding_days END,
       CASE WHEN r.total_trades = m.trade_count THEN r.total_return_pct   ELSE m.return_pct        END,
       (r.total_trades = m.trade_count) IS TRUE
  FROM universe_shrink_metrics m
  LEFT JOIN strategy_backtest_runs r ON r.run_id = m.run_id
 WHERE m.regime_state = 'TOTAL';
```
Rationale: a ladder tier's trades are a SUBSET of the full run's trades (`bucket_trades` filters), so an equal count means the identical trade set, and the only difference is methodology — the run row is the methodology of record. Rows with different counts pass through unchanged (fixed by Part B). `from_run_row` lets consumers log/display the substitution.

Consumers switch table → view (SQL text only, same columns):
- `src/backtest/activation_assigner.py` `_fetch_backtest_rows` (log `… from run rows for K/N regimes` when any `from_run_row`).
- `src/execution/strategy_weights.py` tier 0 query.
- `src/channels/api/server.js` chosen-universe overlay query (`shrinkChosenRows`); pass `from_run_row` through as `entry.regime_breakdown[r].from_run_row` / `entry.shrink_from_run_row` (informational; no UI change required).
Applied with `node -e "require('./src/database/postgres').migrate()"` from `/root/openclaw` (idempotent `CREATE OR REPLACE`), not by a johnbot restart.

Expected effect (measured 11-24): S_beta_spy LOW_VOL reads +0.94 again; 537 identical-count cells across 106 strategies revert to run-of-record numbers at the next activation/weights run; the Strategies page rows/scope blends agree with the chart for those cells.

## 4. Part B — marks-based shrink + parity guard
`scripts/run_universe_shrink.py`:
1. Load trades with `ticker, direction, entry_date, exit_date, pnl_pct, holding_days, entry_regime` from `trades_source()`.
2. `prices = backtest_panel.load_prices(sorted({t.ticker}))` (filtered pyarrow read, ~0.3 GB for a 300-ticker strategy; the full-universe strategies with 20k+ trades read more — run serialized, `nice -n 19`, MemoryMax 3000M). Build `closes_for(ticker) -> pd.Series` (close indexed by datetime, ascending) and call `backtest_panel.prepare_trades_for_curve(trades, closes_for)` BEFORE `shrink_and_select`. Trades whose marks cannot be built fall back to the calendar-span smear exactly as the panel does.
3. Parity guard in the script after `shrink_and_select`: `full_total = result['totals_by_tier'][FULL_TIER]['sharpe']`; compare with `strategy_backtest_runs.total_sharpe` for the run. If both are finite and `abs(diff) > OPENCLAW_SHRINK_PARITY_TOL` (env, default 0.10), print `[shrink] PARITY FAIL <sid> full=<x> run=<y> — rows NOT written`, count it, and `continue` (no metrics rows, no rec, chosen flags untouched). Summary line gains `parity_fail=N`; the Discord post lists them.
4. `universe_shrink.shrink_and_select` is unchanged in signature; a new unit test proves that trades carrying `daily_marks` are aggregated on marks (compare `totals_by_tier['full']['sharpe']` to `aggregate_metrics(prepared)`), and the script-level `_prepare_for_shrink(trades, closes_for)` helper (new, in `src/backtest/universe_shrink.py` so it is testable) is covered with a synthetic close path.
5. Re-run (operator-authorized, after 23:30Z, serialized): `PYTHONPATH=src nice -n 19 python3 scripts/run_universe_shrink.py --force --states live,candidate` (NO `--adopt`: universe changes stay recommendations for operator review), then `python3 -m backtest.activation_assigner --all --notify --trigger=shrink_marks_fix`, then `python3 -m execution.strategy_weights --rebuild --trigger=shrink_marks_fix`. Log `logs/shrink_marks_rerun_20261124.log`. Expected: ≈ 58 cells deactivate, ≈ 23 activate; `parity_fail` should be ~0 (marks from closes vs fills differ by ≈ 0.04 on S_ast_earnings).

## 5. Non-goals
No change to `aggregate_metrics`, `blend_metrics`, `select_tier`, the ladder definitions, the bench vector, or the Strategies page layout. No universe adoption in this pass.

## 6. Risks
- Memory: `load_prices` for the widest strategies (S_ast_low_volatility_factor_effect_in_stocks: 24k trades) may read a few GB — the script must process strategies one at a time and drop `prices` between them; MemoryMax 3000M on the unit; if a strategy OOMs the unit continues with the next (per-strategy try/except already exists? — implementer verifies; add one if not).
- The view's TOTAL branch keys on `total_trades` equality; a ladder tier with the same total but different regime split cannot happen (subset argument) — same reasoning as regimes.
