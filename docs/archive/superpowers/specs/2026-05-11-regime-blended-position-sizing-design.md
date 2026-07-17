# Regime-Blended Position Sizing — Design

**Date**: 2026-05-11
**Status**: Approved design, awaiting implementation plan
**Replaces**: `src/execution/deterministic_sizer.py` as the canonical sizer (kept on disk for parity testing only)

## Context

The current production sizer (`deterministic_sizer.py`, just patched in today's session) sizes every TradeJohn signal independently using half-Kelly with a 5% NAV floor. This has three structural issues:

1. **Position stacking on long-horizon strategies.** A strategy that historically holds positions for 2.2 days currently fires fresh entries every weekday — by the time the original position closes, multiple stacked entries have compounded the same view, multiplying both upside and risk well beyond what the strategy was designed to express.
2. **Per-signal sizing leaves cross-strategy alpha on the table.** Three strategies independently flagging AAPL long emit three separate orders. Each gets sized as if it were the only voice; the natural Bayesian information-aggregation across strategies — three independent confirmations should generate a *larger* position than one — is discarded.
3. **TradeJohn is misallocated.** Today TradeJohn trims signals upstream (chooses which to trade), then the sizer runs blind to the LLM's nuance. TradeJohn's news/historical-context judgment would be more valuable applied to a *consolidated per-ticker proposal* than to raw individual signals.

The fix is a regime-blended sizer that:
- Fires each strategy on a cadence matching its avg holding period (no stacking).
- In low-volatility regimes, consolidates per-ticker alpha and uses TradeJohn as the final per-ticker confirmer.
- In high-volatility regimes, runs strategies independently with mechanical Opus-allocated weights and no LLM regulation.

## Locked design decisions

| # | Decision | Value |
|---|---|---|
| 1 | Independent firing cadence | Per-strategy avg holding period (live exit-time stats from `signal_pnl`, fallback to static `EXPECTED_HOLDING_PERIODS`) |
| 2 | Regime → mode mapping | Binary switch: LOW_VOL/TRANSITIONING → consolidate; HIGH_VOL/CRISIS → independent |
| 3 | ER Weight definition | `strategy_memo_multiplier × signal_kelly_fraction` (hybrid: weekly Opus weight × per-signal Kelly) |
| 4 | "Available Cash" | Reg-T buying power (`regt_buying_power` from Alpaca account snapshot) |
| 5 | Liquidity Parameter by Regime (λ) | Single 0–1 capital-deployment fraction per regime: LOW_VOL=1.00, TRANSITIONING=0.75, HIGH_VOL=0.50, CRISIS=0.25. **Applies in BOTH consolidate and independent paths** — layered defence on top of Opus's `target_pct_nav` in HIGH_VOL/CRISIS. |
| 6 | Consolidated bracket | Direction-leader (largest-notional signal in winning direction) + position-level circuit breaker (-2% NAV per ticker by default) |
| 7 | Per-strategy P&L attribution | Notional-weighted across all contributing signals (winning-side strategies accrue positive, losing-side accrue negative) |
| 8 | Integration approach | Greenfield rewrite; old sizer retained 30 trading days in DRY-RUN parity |
| 9 | Strategy → regime assignment | Explicit `eligible_regimes` field per strategy in `manifest.json`, **derived from per-regime backtest performance** (not literature/manual curation). Set automatically at strategy promotion via `regime_performance_analyzer`; refreshable on every Saturday comprehensive-review. Regime-eligibility filter applied at signal-computation time, NOT at sizer step |
| 10 | HIGH_VOL/CRISIS sizing | Mechanical with layered defence: `qty = (target_pct_nav × NAV × λ_regime) / entry_price` from `strategy_sizing_recommendations` (Opus weekly Saturday output). No Kelly, no TradeJohn, no consolidation. λ provides additional regime-aware brake on top of Opus weights |
| 11 | TradeJohn role in LOW_VOL/TRANSITIONING | Confirmer with veto: per-ticker `{action: approve|veto|scale, multiplier: 0–2, rationale}`. Mostly hands-off; intervenes only on news/history concerns. Fail-OPEN on LLM unavailability (formula-result rides through at multiplier=1.0) |

## Architecture

### Module overview

`src/execution/regime_blended_sizer.py` becomes the single sizer the pipeline orchestrator's `trade` step calls. The existing `deterministic_sizer.py` is **NOT deleted** — kept on disk and invoked in parallel in DRY-RUN mode for 30 trading days so we can diff order outputs and validate parity in HIGH_VOL/CRISIS regimes.

Four new modules under `src/execution/` and one strategy-side helper:

| Module | Approx LOC | Purpose |
|---|---|---|
| `src/execution/regime_blended_sizer.py` | ~250 | Orchestrator. Mode dispatch on regime. |
| `src/execution/signal_cadence_gate.py` | ~120 | Per-strategy firing scheduler. Filters today's signals to cadence-eligible strategies. |
| `src/execution/ticker_consolidator.py` | ~180 | Pure function. Groups signals by ticker, applies the consolidation formula, picks direction-leader bracket. |
| `src/execution/tradejohn_confirmer.py` | ~150 | LLM call. Per-ticker approve/veto/scale in LOW_VOL/TRANSITIONING only. |
| `src/strategies/regime_gate.py` | ~40 | `is_eligible(strategy_id, regime_state) → bool`. Called by every strategy at top of `compute_signals()`. |

### New schemas (migration `069_regime_blended_sizer.sql`)

```sql
-- Per-regime sizing params; tunable without code changes.
CREATE TABLE regime_sizer_params (
  regime_state TEXT PRIMARY KEY,
  liquidity_param REAL NOT NULL CHECK (liquidity_param BETWEEN 0 AND 1),
  min_signal_notional_usd REAL NOT NULL,
  position_circuit_breaker_pct REAL NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO regime_sizer_params VALUES
  ('LOW_VOL',       1.00, 100, 0.020),
  ('TRANSITIONING', 0.75, 100, 0.015),
  ('HIGH_VOL',      0.50, 200, 0.010),
  ('CRISIS',        0.25, 500, 0.005);

-- Per-strategy attribution of consolidated positions.
CREATE TABLE consolidation_contributions (
  consolidated_signal_id BIGINT NOT NULL,
  contributing_signal_id BIGINT NOT NULL,
  strategy_id TEXT NOT NULL,
  signal_position_size_usd REAL NOT NULL,
  attribution_weight REAL NOT NULL,     -- abs(signed_size) / sum_abs(signed_size)
  contributed_direction INT NOT NULL,   -- +1 / -1
  created_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (consolidated_signal_id, contributing_signal_id)
);
-- Constraint: SUM(attribution_weight) per consolidated_signal_id = 1.0 (enforced at write time).

-- Cadence-skip audit (forensic + dashboard).
CREATE TABLE cadence_skips (
  id BIGSERIAL PRIMARY KEY,
  signal_date DATE NOT NULL,
  strategy_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  reason TEXT NOT NULL,                 -- e.g. cadence_pending_until_2026-05-15, net_zero_after_consolidation
  context_json JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_cadence_skips_date ON cadence_skips (signal_date DESC);

-- DRY-RUN parity output (30-day comparison).
CREATE TABLE parity_orders (
  id BIGSERIAL PRIMARY KEY,
  signal_date DATE NOT NULL,
  ticker TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('regime_blended', 'deterministic')),
  qty REAL NOT NULL,
  notional_usd REAL NOT NULL,
  bracket_json JSONB NOT NULL,
  contributing_signal_ids BIGINT[],
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_parity_orders_date_ticker ON parity_orders (signal_date DESC, ticker);

-- Intraday circuit-breaker audit.
CREATE TABLE circuit_breaker_fires (
  id BIGSERIAL PRIMARY KEY,
  ts_utc TIMESTAMPTZ NOT NULL,
  ticker TEXT NOT NULL,
  unrealized_pnl_pct_nav REAL NOT NULL,
  threshold_pct REAL NOT NULL,
  position_qty REAL NOT NULL,
  close_result_json JSONB NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Per-strategy attribution carrier on signal_pnl.
ALTER TABLE signal_pnl ADD COLUMN attribution_weight REAL DEFAULT 1.0;

-- Per-strategy cadence state.
CREATE TABLE strategy_state (
  strategy_id TEXT PRIMARY KEY,
  last_fire_date DATE,
  next_fire_date DATE,
  avg_holding_days REAL,
  source TEXT,                          -- 'live_signal_pnl' | 'static_fallback' | 'bootstrap_daily'
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Manifest schema addition

Each live strategy in `src/strategies/manifest.json` gains:

```json
{
  "eligible_regimes": ["LOW_VOL", "TRANSITIONING"]
}
```

Strategies missing this field default to all-four (backward compat). Validation at strategy-load enforces subset of `{LOW_VOL, TRANSITIONING, HIGH_VOL, CRISIS}`.

## Data flow

### Daily 10:00 ET cycle

1. `collect` — unchanged (Polygon/FMP/Alpaca pulls).
2. `signals` — **revised**: each strategy reads `regime_latest.json` first; if current regime ∉ `eligible_regimes`, the strategy short-circuits via `regime_gate.is_eligible()` and writes zero rows.
3. `handoff` — unchanged (HV/regime/confidence enrichment + per-ticker news headlines for downstream LLM context).
4. **TradeJohn upstream signal-trimming role is DELETED**.
5. **`trade`** (re-wired) — calls `regime_blended_sizer.size_positions()`:
   - Load `regime_latest.json` and Alpaca account snapshot.
   - **Cadence gate** (`signal_cadence_gate.filter`): drop signals where `strategy.next_fire_date > today`. Skipped reasons go to `cadence_skips`.
   - **Mode dispatch**:
     - **HIGH_VOL or CRISIS** → independent path:
       - For each cadence-passed signal, look up `target_pct_nav` from latest `strategy_sizing_recommendations`.
       - `qty = (target_pct_nav × NAV × λ_regime) / entry_price` — λ is the layered-defence brake on top of Opus weights.
       - Use signal's own bracket.
       - Emit one order per signal. **No Kelly, no TradeJohn, no consolidation.**
     - **LOW_VOL or TRANSITIONING** → consolidate path:
       - `ticker_consolidator.consolidate(signals)` produces per-ticker preliminary sizes via `SignalPositionSize = ER_Weight × regt_buying_power × λ(regime)`, sums by ticker.
       - Build TradeJohn context dict per ticker: `{ticker, preliminary_size_usd, contributing_signals[{strategy_id, kelly_p, bracket, direction, ev}], news_headlines, 30d_veto_history_for_ticker, sector, hv30d}`.
       - **`tradejohn_confirmer.confirm(proposals)`** — single batched LLM call covering all tickers. Returns per-ticker `{action, multiplier, rationale}`.
       - `final_size_usd = preliminary_size_usd × multiplier` (0 if vetoed).
       - Direction-leader bracket applies; per-strategy attribution rows written to `consolidation_contributions`.
       - Emit one order per ticker.
6. **`trade_parity`** (DRY-RUN, 30 days) — runs old `deterministic_sizer` on the same raw signal set; outputs to `parity_orders`; daily 21:00 UTC diff job posts ticker-level discrepancies to `#botjohn-log`.
7. `alpaca` — unchanged (submits via `alpaca order submit` CLI).
8. `report`, `health` — unchanged shape; gain a regime-mode line and TradeJohn intervention summary.

### Per-strategy bookkeeping

After orders emit, update `strategy_state.last_fire_date = today` for every strategy that contributed (in either mode). TradeJohn vetoes in consolidate mode still advance `last_fire_date` — same veto would likely recur on the next cadence anyway.

### Daily 23:55 ET — `strategy_cadence_recompute` (new cron)

- Refresh per-strategy avg holding period from `signal_pnl` (live exit-time stats over rolling window of last 30 closed trades).
- Update `strategy_state.next_fire_date = last_fire_date + ceil(avg_holding_days)`.
- New strategies with < 5 closed trades use static `EXPECTED_HOLDING_PERIODS` lookup (`source='static_fallback'`).
- Strategies missing from both default to daily cadence (`source='bootstrap_daily'`).
- One-line summary to `#botjohn-log`.

### Intraday 5-min — `position_circuit_breaker` (new cron)

- Pulls each open consolidated-mode position's current P&L via `alpaca position list`.
- Computes `unrealized_pnl_pct = (mark - entry) × qty / NAV`.
- If `|unrealized_pnl_pct| > position_circuit_breaker_pct` (from `regime_sizer_params`): closes via existing `_close_symbol()`; writes `circuit_breaker_fires` row; posts to `#trade-reports`.
- Independent-mode positions skip — strategy-level brackets are their cutoff.

### Regime-transition liquidation (existing, unchanged)

- Liquidator still flattens on transition (daily 9am or intraday HMM).
- Post-liquidation, next 10am cycle re-fires from cadence-and-regime-eligible strategies under the new regime's mode.
- A strategy may participate in *consolidate mode* before the transition and *independent mode* after — its `eligible_regimes` covers both, so it's not dropped.
- `last_fire_date` is **not** advanced by a liquidation event; only by an emitted order.

## Error handling

### Per-signal validity (carried over from existing sizer)

| Condition | Reason | Action |
|---|---|---|
| `R ≤ 0` (malformed bracket) | `malformed_signal_R<=0` | Veto; not in `consolidation_contributions` |
| Negative Kelly | `negative_kelly` | Veto |
| Repeat offender (>5 vetoes / 30d / ticker) | `repeat_offender_d-1` | Veto |

### Consolidation edge cases

| Condition | Action |
|---|---|
| `TickerPositionSize ≈ 0` | `cadence_skips` reason `net_zero_after_consolidation`; no order |
| Single-signal ticker | Degenerate to per-signal; attribution=1.0 to single contributor |
| **Direction-leader tie** | **`direction_tie_net_zero`; no order, audit row written** |

### TradeJohn confirmer failures

| Condition | Action |
|---|---|
| LLM timeout / total unavailable | **Fail-OPEN**: every ticker defaults to `approve, multiplier=1.0`; formula-result rides through; `:warning:` to `#botjohn-log` |
| Malformed JSON for some tickers | Those tickers fail-open at `multiplier=1.0`; valid responses honored |
| Budget cap exceeded mid-call | Tickers not yet processed default to formula-result; alert |

### HIGH_VOL/CRISIS sizing

| Condition | Action |
|---|---|
| Strategy missing from `strategy_sizing_recommendations` | Fallback 1% NAV/signal; warn `missing_strategy_sizing` |
| `target_pct_nav = 0` | Skip signal; veto reason `opus_sized_to_zero` |

### Cadence gate failures

| Condition | Action |
|---|---|
| `signal_pnl` query failure | Fall back to static `EXPECTED_HOLDING_PERIODS`; doctor flags Postgres |
| Strategy < 5 closed trades and not in static dict | Default to daily cadence; log `cadence_bootstrap_daily` |

### Position circuit breaker failures

| Condition | Action |
|---|---|
| `position list` call fails | No-op tick; logged but not fatal |
| Close-order submission fails | Retry once; if still failing, alert `#trade-reports`; do NOT mark breakered (next tick re-evaluates) |

### Regime-state read failure

`regime_latest.json` missing/corrupt → `trade` step bails with `regime_unreadable`. Same fail-mode as today's `regime_liquidator`. Doctor catches pre-flight.

### Consistency invariants

- `consolidation_contributions.attribution_weight` per `consolidated_signal_id` must sum to 1.0 (enforced at write time).
- `regime_sizer_params.liquidity_param ∈ [0, 1]` (CHECK constraint).
- `manifest.eligible_regimes` must be a subset of `{LOW_VOL, TRANSITIONING, HIGH_VOL, CRISIS}` (validated at strategy load).

## Testing strategy

### Unit tests (pure functions, no DB or broker)

- `tests/test_ticker_consolidator.py` (~25 tests): grouping, netting, direction-leader, λ application, ER_Weight math, net-zero skip, direction-tie net-zero, sub-min-notional skip.
- `tests/test_signal_cadence_gate.py` (~15 tests): avg-holding cadence math, fallback paths, last_fire_date advancement rules (including TradeJohn veto and post-liquidation cases).
- `tests/test_regime_gate.py` (~10 tests): eligibility match, missing field default, malformed value default.
- `tests/test_tradejohn_confirmer.py` (~12 tests, mocked LLM): approve/veto/scale application, multiplier=0 → veto, malformed JSON per-ticker fail-open, total timeout fail-open, prompt schema regression.

### Integration tests (full pipeline, mocked broker, real Postgres + Redis)

- `test_low_vol_consolidate_cycle.py`: 8 strategies × 5 tickers, mocked TradeJohn approve-all → 5 orders + correct attribution rows.
- `test_high_vol_independent_cycle.py`: 4 strategies × 6 signals, no consolidation, no TradeJohn, sizes from `strategy_sizing_recommendations` → 6 orders.
- `test_regime_transition_mid_day.py`: LOW_VOL 10am → CRISIS 11am intraday HMM → liquidation → next-day 10am in independent mode.
- `test_cadence_post_liquidation.py`: strategy fires Mon, liquidated Tue mid-day, Wed cycle honors original cadence.
- `test_circuit_breaker_fires.py`: consolidate-mode position drops -2.1% NAV → breaker closes + audit + Discord.

### Parity tests (30-day DRY-RUN diff)

- `tests/test_parity_diff.py`: in HIGH_VOL/CRISIS regimes, new sizer's order book matches old sizer's within 1% per ticker.
- Nightly 21:00 UTC `output/sizer_parity_<date>.json` + summary line to `#botjohn-log`.
- Diffs > 1% raise `:warning:` alert.

### Smoke test

- `scripts/dry_run_new_sizer.py` — load today's actual signals, run new sizer in DRY-RUN, print planned order book without submitting. Run after each PR.

### Backtest harness

- `src/backtest/regime_blended_backtest.py` (~300 lines) — walk-forward over 2y of `signal_pnl` with synthetic regime feed.
- Outputs `output/regime_blended_walkforward.json` with: aggregate Sharpe, max DD, fire-frequency-per-strategy, mode-distribution-by-day, TradeJohn-veto-rate proxy.
- Primary signal for the LIVE-flag flip in Phase 3.

## Migration / rollout

### Phase 0 — schema + scaffolding (1 PR, no behavior change)

- Migration `069_regime_blended_sizer.sql` — all new tables and columns.
- All new code added but unused; pipeline still calls `deterministic_sizer`.
- `regime_sizer_params` seeded with the four-row insert above.
- `strategy_state.last_fire_date` initialized one-time from `MAX(signal_date)` per strategy in `execution_signals`.
- `signal_pnl.attribution_weight=1.0` backfilled for all historic rows.

### Phase 1 — manifest backfill (automated, derived from backtests)

- New module `src/backtest/regime_performance_analyzer.py` runs over each live strategy's last 2 years of `signal_pnl` (or backtest output if live history < 30 trades), partitioned by the regime in effect at each signal_date.
- For each (strategy, regime) pair, compute: Sharpe, win-rate, trade-count, avg-R-multiple. A regime is added to `eligible_regimes` if `Sharpe ≥ 0.5 AND trade_count ≥ 20 AND avg_R_multiple > 0`. Tunable thresholds in `regime_eligibility_thresholds` config table.
- Output `output/regime_eligibility_<date>.json` with per-strategy proposed assignments + the underlying stats.
- Operator reviews the JSON, optionally overrides any assignment manually, then a one-shot script writes the final `eligible_regimes` field into `manifest.json`.
- Commit updated manifest as a separate PR.
- A strategy with no qualifying regimes is **not auto-archived**; instead its `eligible_regimes` is set to `[]` and a warning lists it for operator review (likely candidate for archive or re-tuning).
- `candidate`/`staging` strategies get assignments at promotion via the same analyzer (see "Strategy creation pipeline changes" below).

### Phase 2 — DRY-RUN deployment (1 PR)

- Pipeline `trade` step runs BOTH sizers; only `deterministic_sizer` output submitted to broker.
- New sizer's output → `parity_orders`.
- Cadence-recompute and circuit-breaker crons live (circuit-breaker in DRY-RUN: logs hypothetical closes).
- TradeJohn confirmer LLM runs against per-ticker shape; output → `tradejohn_decisions_dryrun` table for post-hoc analysis.
- Daily 21:00 UTC parity diff to `#botjohn-log`.
- Flag: `OPENCLAW_REGIME_BLENDED_LIVE=0` (default, DRY-RUN).

### Phase 3 — operator gate to LIVE

- After ≥ 30 trading days of clean parity in HIGH_VOL/CRISIS regimes (matched orders within 1% per ticker) AND backtest harness shows positive Sharpe delta:
  - Flip `OPENCLAW_REGIME_BLENDED_LIVE=1` in `.env`.
- New sizer's output now submits. Old sizer keeps running in DRY-RUN as rollback canary for another 30 days.
- Circuit breaker flips to LIVE simultaneously.

### Phase 4 — old sizer retirement (1 PR, after 60 trading days total)

- Remove `trade_parity` step. `deterministic_sizer.py` stays on disk; no longer invoked.
- Remove `OPENCLAW_REGIME_BLENDED_LIVE` flag (always-on).
- Update CLAUDE.md.

### Rollback

- Phase 2: zero risk — new code never reaches broker.
- Phase 3: flip `OPENCLAW_REGIME_BLENDED_LIVE=0`. Old sizer becomes live submitter on next cycle.
- Phase 4: revert PR. Old sizer is still on disk.

### TradeJohn prompt rewrite

- New prompt at `src/agent/prompts/subagents/tradejohn-confirmer.md`. Input shape: per-ticker proposals. Output schema: `{ticker: {action, multiplier, rationale}}` JSON.
- Lives alongside existing `src/agent/prompts/subagents/tradejohn.md` (kept for reference).
- Token-budget guard at call site: if estimated input > 25K tokens, drop lowest-conviction tickers (formula-result rides through for those) until under budget.

### Crons added (`src/engine/cron-schedule.js`)

- `cron.schedule('55 23 * * *', strategy_cadence_recompute, { timezone: 'America/New_York' })`
- `cron.schedule('*/5 9-16 * * 1-5', position_circuit_breaker, { timezone: 'America/New_York' })`
- `cron.schedule('0 21 * * 1-5', parity_diff_report)` (UTC)

## Strategy creation pipeline changes

Regime-eligibility derivation must be a first-class step in every path that adds or revisits a strategy, not a one-time backfill. Three pipelines need to gain the regime-eligibility step:

### A. PaperHunter → StrategyCoder → Promotion (new strategy creation)

- **PaperHunter** (`src/agent/prompts/subagents/paperhunter.md`): no change — extraction stays focused on idea + parameters.
- **StrategyCoder** (`src/agent/prompts/subagents/strategycoder.md`): the implementation prompt gains a requirement that the generated strategy includes a regime-aware backtest harness call (uses existing `src/backtest/quick_backtest.py` extended to partition by regime). Strategies that fail to produce regime-partitioned output cannot pass the lifecycle gate to `staging`.
- **Promotion gate** (`src/strategies/lifecycle.py`): a new check `validate_regime_eligibility_present()` runs before any `candidate → staging` transition. The gate calls `regime_performance_analyzer.analyze(strategy_id)`; if at least one regime qualifies, the result is written to `manifest.json` and the strategy advances. If none qualify, the strategy stays in `candidate` with a `requires_regime_qualification` blocker logged.

### B. Saturday comprehensive-review (live strategy refresh)

- `src/agent/curators/comprehensive_review.js` already runs a per-strategy lifetime review every Saturday 18:00 ET. Extend it to:
  - Re-run `regime_performance_analyzer.analyze()` for each live strategy using the latest 90 days of `signal_pnl`.
  - Compare against the strategy's current `eligible_regimes`. If a regime no longer qualifies (e.g., live performance has degraded), the analyzer flags the change but does NOT auto-modify the manifest — operator review required (writes a `regime_eligibility_drift` row to `strategy_memos`, which Opus surfaces in the Saturday memo).
  - If a NEW regime newly qualifies, same: surface for operator review, don't auto-modify.
- Manual `eligible_regimes` updates via a one-shot CLI: `python scripts/update_eligible_regimes.py --strategy <id> --add HIGH_VOL` (or `--remove`).

### C. Backtesting infrastructure

- `src/backtest/quick_backtest.py` extended: every backtest run now partitions results by the regime in effect at each signal_date. Adds two columns to `quick_backtest`'s return dict: `regime_partition: {LOW_VOL: {...}, ...}` and `eligible_regimes_proposed: ['LOW_VOL', 'TRANSITIONING']`.
- `src/backtest/regime_performance_analyzer.py` (new, ~200 lines): the canonical analyzer. Reads either backtest output OR live `signal_pnl`, applies thresholds from `regime_eligibility_thresholds` config table, returns proposed `eligible_regimes` list + supporting stats.
- New table `regime_eligibility_thresholds` (Phase 0 migration):
  ```sql
  CREATE TABLE regime_eligibility_thresholds (
    threshold_name TEXT PRIMARY KEY,
    value REAL NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
  );
  INSERT INTO regime_eligibility_thresholds VALUES
    ('min_sharpe',        0.5, NOW()),
    ('min_trade_count',  20.0, NOW()),
    ('min_avg_r',         0.0, NOW());
  ```

### D. Per-regime live-performance metric (overlap with existing concept)

- Live regime-performance metrics are already computed somewhere in the system (operator-noted overlap with `strategy_state` concept).
- The new `regime_performance_analyzer` becomes the canonical source. The existing live-metric computation should call into the analyzer so there's one definition of "per-regime Sharpe" rather than two drifting versions.
- During Phase 0, identify the existing computation site and refactor it to import from `regime_performance_analyzer`. If duplicated logic remains after Phase 0, flag for cleanup in Phase 4.

## Critical files

| File | Action | Purpose |
|---|---|---|
| `src/execution/regime_blended_sizer.py` | new | Orchestrator; mode dispatch |
| `src/execution/signal_cadence_gate.py` | new | Per-strategy cadence filter |
| `src/execution/ticker_consolidator.py` | new | Pure consolidation function |
| `src/execution/tradejohn_confirmer.py` | new | LLM call (LOW_VOL/TRANSITIONING only) |
| `src/strategies/regime_gate.py` | new | `is_eligible(strategy_id, regime_state)` |
| `src/database/migrations/069_regime_blended_sizer.sql` | new | All new tables + columns + `regime_eligibility_thresholds` |
| `src/strategies/manifest.json` | modify | Add `eligible_regimes` per live strategy (auto-derived from analyzer) |
| `src/execution/pipeline_orchestrator.py` | modify | Re-wire `trade` step + add `trade_parity` step |
| `src/engine/cron-schedule.js` | modify | Three new cron lines |
| `src/agent/prompts/subagents/tradejohn-confirmer.md` | new | Per-ticker confirmer prompt |
| `src/agent/prompts/subagents/strategycoder.md` | modify | Require regime-partitioned backtest output |
| `src/agent/curators/comprehensive_review.js` | modify | Saturday refresh of regime-eligibility drift detection |
| `src/strategies/lifecycle.py` | modify | New `validate_regime_eligibility_present()` gate at candidate→staging |
| `src/backtest/quick_backtest.py` | modify | Partition output by regime; add `regime_partition` + `eligible_regimes_proposed` to return dict |
| `src/backtest/regime_performance_analyzer.py` | new | Canonical per-regime Sharpe/win-rate/R-multiple analyzer |
| `scripts/update_eligible_regimes.py` | new | One-shot CLI for manual override |
| `src/execution/deterministic_sizer.py` | unchanged | Kept for parity DRY-RUN; retired in Phase 4 |
| `tests/test_ticker_consolidator.py` | new | ~25 unit tests |
| `tests/test_signal_cadence_gate.py` | new | ~15 unit tests |
| `tests/test_regime_gate.py` | new | ~10 unit tests |
| `tests/test_tradejohn_confirmer.py` | new | ~12 unit tests |
| `tests/integration/test_low_vol_consolidate_cycle.py` | new | End-to-end consolidate path |
| `tests/integration/test_high_vol_independent_cycle.py` | new | End-to-end independent path |
| `tests/integration/test_regime_transition_mid_day.py` | new | Mid-day regime transition |
| `tests/integration/test_cadence_post_liquidation.py` | new | Cadence preserved across liquidation |
| `tests/integration/test_circuit_breaker_fires.py` | new | Circuit breaker end-to-end |
| `tests/test_parity_diff.py` | new | DRY-RUN parity validation |
| `src/backtest/regime_blended_backtest.py` | new | 2y walk-forward harness |
| `scripts/dry_run_new_sizer.py` | new | Manual smoke test |
| CLAUDE.md | modify | Document new flow at Phase 4 |

## Verification

End-to-end checks that gate each phase:

1. **Phase 0**: `pytest tests/test_*.py -v` for the four unit-test files; all green. Migration applies cleanly to a fresh DB.
2. **Phase 1**: manifest contains `eligible_regimes` for all 53 live strategies; subset validation passes at strategy-load.
3. **Phase 2**: 5 trading days of DRY-RUN with `parity_orders` populated; daily diff jobs post to `#botjohn-log`.
4. **Phase 3 gate**: ≥ 30 trading days of clean parity in HIGH_VOL/CRISIS; walk-forward backtest shows Sharpe delta > 0 and max-DD delta ≥ 0; operator flips `OPENCLAW_REGIME_BLENDED_LIVE=1`.
5. **Phase 4**: 60+ trading days post-LIVE with no rollback events; PR removes parity step.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| TradeJohn fail-open lets bad sizes through during LLM degradation | Daily digest reports % fail-open events; alert if >10% in a week |
| Per-strategy attribution math depends on accurate `consolidation_contributions` writes | CHECK constraint enforces sum=1.0 per consolidated_signal_id |
| Cadence-vs-liquidation interaction surprises in production | Explicit integration test (`test_cadence_post_liquidation`) |
| 30-day parity window assumes HIGH_VOL/CRISIS regime appears | If no HIGH_VOL/CRISIS in 30 days, extend window OR run synthetic regime test |
| Manifest backfill is manual operator work for 53 strategies | Default to all-four for strategies without clear research signal; refine over time |
| New strategy promoted with no `eligible_regimes` field | Backward compat: defaults all-four; warning logged at first signal compute |
| Token-budget blowout in TradeJohn confirmer | 25K input cap with lowest-conviction-ticker dropping; per-call cost ceiling enforced |
| Circuit-breaker false positives in fast markets | -2% NAV threshold tunable per regime; circuit breaker only checks consolidate-mode positions |
| `signal_pnl` exit-time stats biased early on (before 30 closed trades / strategy) | Static `EXPECTED_HOLDING_PERIODS` fallback; bootstrap-daily for absent strategies |

---

## Addendum — 2026-05-12: dropped 30-day DRY-RUN gate; operator-driven flip

**Policy update (supersedes Phase 3 gate language above):** Per operator decision 2026-05-12, the 30-day DRY-RUN parity calendar gate is dropped from the rollout plan. The operator monitors system + performance themselves and flips `OPENCLAW_REGIME_BLENDED_LIVE=1` when ready — no automated readiness gate, no calendar countdown.

**Why:** The 30-day window was a calendar gate, not an evidence gate. Validating "the new sizer matches `trade_agent_llm` within 1%" was always weak — `trade_agent_llm` itself is non-deterministic and has its own failure modes, so "matches the imperfect baseline" doesn't validate the new sizer specifically. Better to let the operator's qualitative judgment, doctor preflight, and live ARR/PnL be the gate.

**What stays running:**
- `parity_diff` (21:00 UTC weekdays) — kept as an audit trail and bug tripwire. If a code regression makes the new sizer propose 800% NAV in one ticker, the diff catches it. Decoupled from any wait.
- Doctor checks `regime_blended_gate_b`, `regime_live_rollup_freshness`, `manifest_eligibility_drift` — informational, not blocking the flip.
- All circuit-breaker and risk-cap logic — unchanged.

**Where the operator UI lives:** Live eligibility editing folded into the strategies page (clickable regime cells on `state='live'` rows). The original separate Regime tab was removed (commit `5f51f1b`) as it duplicated data already on the strategies page.

**LIVE flip procedure (replaces Phase 3 gate above):**
1. `OPENCLAW_REGIME_BLENDED_LIVE=1` in `/root/openclaw/.env`
2. `systemctl restart johnbot.service`
3. Watch next 14:00 UTC cycle in `#botjohn-log`
4. Rollback: flip back to `0`, restart.

Original Phase 3 gate language preserved above for historical record.
