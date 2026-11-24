# Shrink Sharpe fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-regime Sharpe consumed by activation, weights and the Strategies page comes from the backtest-of-record methodology.

**Architecture:** Part A: a SQL view substitutes run-of-record rows for chosen shrink rows with identical trade counts; three consumers switch to the view. Part B: the shrink script attaches true daily marks to trades before aggregating and refuses to write rows that fail a parity check against the run.

**Tech Stack:** PostgreSQL view (migration 170), Python (psycopg2, pandas, numpy), Node (server.js), pytest + node --test.

**Spec:** docs/superpowers/specs/2026-11-24-shrink-sharpe-marks-fix-design.md

## Global Constraints
- View name `universe_shrink_metrics_effective`; columns exactly: id, strategy_id, run_id, tier, regime_state, chosen, candidate_set_id, rec_id, computed_at, trade_count, sharpe, max_dd_pct, win_rate, sortino, calmar, mean_holding_days, return_pct, from_run_row.
- Substitution rule: regime rows ⇔ `strategy_backtest_regimes.trade_count = m.trade_count`; TOTAL rows ⇔ `strategy_backtest_runs.total_trades = m.trade_count`.
- Parity tolerance env `OPENCLAW_SHRINK_PARITY_TOL`, default 0.10; on failure NO rows/rec written for that strategy.
- Never run the full test suite (2-core box); run the named test files only. Tests on this box reach the REAL DB — stub cursors in unit tests (existing pattern in tests/backtest/test_activation_assigner.py).
- Commit paths by name; never commit `.superpowers/`; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus
1. A chosen shrink row with NULL sharpe but equal trade_count → view returns the run row's value (may also be NULL) — consumers must treat NULL as before.
2. A regime absent from `strategy_backtest_regimes` for the run (older runs) → LEFT JOIN keeps the shrink row, `from_run_row` false.
3. Parity guard must compare only when both Sharpes are finite; a NULL run total (std floor) must not count as failure.
4. `prepare_trades_for_curve` with a ticker missing from prices → smear fallback, not a crash; `bucket_trades` must still see `ticker`/`entry_date`.
5. Memory: prices for one strategy are released before the next strategy is processed.

---

### Task 1: View migration + consumers (Part A)
**Files:** Create `src/database/migrations/170_universe_shrink_metrics_effective.sql`; Modify `src/backtest/activation_assigner.py` (`_fetch_backtest_rows`), `src/execution/strategy_weights.py` (tier 0 query ~L410-420), `src/channels/api/server.js` (`shrinkChosenRows` query ~L1762); Tests: `tests/backtest/test_activation_assigner.py` (existing assertion on table name → view name), `tests/backtest/test_shrink_effective_view.py` (new, read-only live-DB check: for S_beta_spy's latest primary run, every chosen row with equal trade_count has `from_run_row` true and `sharpe` equal to the regimes row; skip if no row).
- [ ] Write the migration exactly as in spec §3. Apply with `cd /root/openclaw && node -e "require('./src/database/postgres').migrate().then(()=>process.exit(0))"` and verify `SELECT count(*) FROM universe_shrink_metrics_effective WHERE from_run_row` > 0.
- [ ] Switch the three queries to the view; activation logs `eligibility from chosen shrink sleeves (N regimes, K from run rows)`; server.js passes `from_run_row` into the overlay entries.
- [ ] Run `pytest -q tests/backtest/test_activation_assigner.py tests/backtest/test_activation_assigner_main.py tests/backtest/test_shrink_effective_view.py tests/execution/test_strategy_weights_tier3_retired.py`, `node --check src/channels/api/server.js`.
- [ ] Commit.

### Task 2: Marks-based shrink + parity guard (Part B, code)
**Files:** Modify `scripts/run_universe_shrink.py`, `src/backtest/universe_shrink.py` (add `prepare_for_shrink(trades, closes_for)` wrapping `backtest_panel.prepare_trades_for_curve`, and `parity_check(full_total_sharpe, run_total_sharpe, tol) -> bool`); Tests: `tests/backtest/test_universe_shrink.py` (add `test_prepare_for_shrink_attaches_marks`, `test_shrink_and_select_aggregates_on_marks`, `test_parity_check_tolerance_and_nulls`).
- [ ] Trades query gains `direction, exit_date`; load prices per strategy via `backtest_panel.load_prices`, build `closes_for`, call `prepare_for_shrink` before `shrink_and_select`; free `prices`/`closes` after each strategy.
- [ ] Parity guard per spec §4.3 with the summary counter and Discord lines.
- [ ] `pytest -q tests/backtest/test_universe_shrink.py tests/backtest/test_universe_grid.py`.
- [ ] Commit.

### Task 3: Re-run (operator-authorized, controller runs after merge, after 23:30Z)
- [ ] `--dry-run --limit 3` smoke on live DB (prints parity + tier lines, writes nothing).
- [ ] Full re-run per spec §4.5 as a transient unit or foreground with nohup; verify `parity_fail`, cell flips, weights rebuilt; record in the ledger.
