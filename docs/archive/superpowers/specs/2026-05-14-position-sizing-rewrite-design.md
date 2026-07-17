# Position-sizing rewrite — Sharpe×cadence weighted consolidation for ALL regimes

**Date:** 2026-05-14
**Status:** draft (awaiting user review)
**Author:** BotJohn (Claude Code)
**Target files:**
  - `src/execution/regime_blended_sizer.py` (rewrite math; drop `_select_mode`)
  - `src/execution/regime_blended_sizer_live.py` (entry point — minor)
  - `src/execution/strategy_weights.py` (new — per-regime weight engine)
  - `src/execution/tradejohn_confirmer.py` + its prompt (narrow to news-cancel only)
  - `src/strategies/lifecycle.py` (auto-demote-on-negative-sharpe hook)
  - `src/database/migrations/090_strategy_weights_by_regime.sql` (new table)
  - `src/database/migrations/091_pipeline_config_lambda.sql` (lambda key seed)
  - `src/agent/curators/weekly_live_sharpe.js` (new — Saturday cron)
  - `src/channels/api/server.js` (lambda dashboard control + weights view)

## Problem statement

The current LIVE sizer (`regime_blended_sizer_live.py`, shipped 2026-05-12) routes between two modes — **Consolidate** (LOW_VOL/TRANSITIONING) and **Independent** (HIGH_VOL/CRISIS) — selected by `_select_mode(regime_state)`. The operator wants:

1. **Consolidate always.** Strategies fire on their own cadence; their signals stay valid through the cadence window; every day, ticker positions are recomputed by aggregating *all currently-valid* strategy signals. The Independent path is retired.
2. **Sharpe×cadence weighting.** Each strategy's contribution to the daily portfolio is its **per-regime Sharpe share** divided by its **cadence in days**. A monthly strategy contributes 1/21 of its weight on any given day; a daily strategy contributes its full weight.
3. **Lambda-capped total notional.** Σ |ticker_position_usd| = `λ × NAV` exactly, where λ is an operator-tunable dashboard parameter. Default λ = 2.0, range 0.10–3.50.
4. **First-day-of-regime universal fire.** Cadence gate bypassed on the first daily cycle of a new regime state — every eligible strategy emits a signal so the new regime starts with a fresh full read.
5. **Weekly live-Sharpe update.** Every Saturday, blend new closed-trade live Sharpe into the per-regime effective Sharpe via sample-size weighting. MasterMindJohn's Saturday review continues separately and only *recommends* changes.
6. **TradeJohn narrows.** Drop approve/scale; the confirmer's sole action is `cancel` on highly alarming ticker news. Never adjusts size.
7. **Auto-demotion.** Any strategy whose effective Sharpe goes ≤ 0 across **all** of its eligible regimes is moved from `live` / `monitoring` back to `candidate` (research stack).

## The math

### Inputs (per active strategy `s`)
- `bt_sharpe(s, R)` — backtest Sharpe in regime R, from `strategy_regime_backtests.sharpe`.
- `bt_n(s, R)` — backtest trade count in regime R, from `strategy_regime_backtests.trade_count`.
- `live_sharpe(s, R)` — live trade-grain Sharpe over closed trades in regime R, computed from `signal_performance` × `execution_signals` × `market_regime` join.
- `live_n(s, R)` — live closed-trade count in regime R.
- `cadence_days(s)` — derived from class attribute `signal_frequency`:
  - `'daily'` → 1
  - `'weekly'` → 5 (trading days)
  - `'monthly'` → 21 (trading days)
  - Anything else → error; lifecycle gate refuses promotion until set.
- `direction(s, t)` ∈ {+1, -1} — for any ticker `t` the strategy currently signals.
- `eligible_regimes(s)` — from manifest `metadata.eligible_regimes`.

### Per-regime effective Sharpe
```
n_total    = bt_n + live_n
effective_sharpe(s, R) =
    (bt_n × bt_sharpe + live_n × live_sharpe) / n_total       if n_total > 0
    else NULL
```

When live data doesn't exist yet (newly-promoted strategy), `live_n = 0` and `effective_sharpe` collapses to `bt_sharpe`. Each weekly update increases `live_n` and the live signal gradually overtakes the backtest.

### Per-regime strategy weight
```
positives(R) = { s : R ∈ eligible_regimes(s) AND effective_sharpe(s, R) > 0 }
denom(R)     = Σ_{s ∈ positives(R)} effective_sharpe(s, R)
w(s, R)      = effective_sharpe(s, R) / denom(R)    for s ∈ positives(R)
               0                                     otherwise
```

`Σ_s w(s, R) = 1.0` over the active stack within R.

### Per-strategy daily weight
```
w_daily(s, R) = w(s, R) / cadence_days(s)
```

Note: `Σ_s w_daily(s, R) ≤ 1.0` (with equality only if every strategy is `daily`).

### Per-ticker daily weight
For each ticker `t`, let `S_t` be the set of strategies in the active stack whose current signal on `t` is still within its cadence window (i.e., `today − last_fire(s) < cadence_days(s)`):

```
ticker_weight(t) = Σ_{s ∈ S_t} w_daily(s, R_today) × direction(s, t)
```

Multiple strategies on the same ticker can cancel each other (one LONG, one SHORT). `ticker_weight(t)` is signed; sign determines the position direction.

### Normalize to λ
```
gross = Σ_t |ticker_weight(t)|
λ_normalizer = λ × NAV / gross   if gross > 0 else 0
position_usd(t) = ticker_weight(t) × λ_normalizer    (signed)
```

After normalization, `Σ_t |position_usd(t)| = λ × NAV` exactly.

### Safety caps (preserved from current system)
- **Per-ticker daily cap:** `|position_usd(t)| ≤ 0.25 × NAV`. If the normalizer would push a single ticker past 25%, clamp it and redistribute the excess proportionally across other tickers.
- **Minimum trade threshold:** `|position_usd(t)| < $25` → drop. Cumulative dropped notional is also redistributed.

## Lifecycle changes

### Auto-demotion
- New lifecycle gate runs on every `strategy_weights` recompute (after each weekly update + after manual ops):
  - For each `live` / `monitoring` strategy, if `effective_sharpe(s, R) ≤ 0` for **every** R in `eligible_regimes(s)`, demote to `candidate` (state = `candidate`, reason = `auto_demote_negative_sharpe`).
  - Emits a Discord notice to `#general` and a `strategy_lifecycle_audit` row.

### Auto-recompute triggers
The per-regime `strategy_weights_by_regime` table is rebuilt whenever:
1. A strategy enters or leaves the active stack (`live` ↔ other states) — hook in `lifecycle.transition`.
2. The Saturday cron runs (weekly live-Sharpe refresh).
3. Operator invokes `python3 -m execution.strategy_weights --rebuild` manually.

## Regime-change first-day behaviour

Today: `regime_liquidator.py` flattens positions on a regime state transition. **Addition:** on the first 10:00 ET cycle following a regime transition, the cadence gate is bypassed: every strategy whose `eligible_regimes` includes the new regime is force-fired regardless of its `last_fire` timestamp.

Implementation: `signal_cadence_gate.filter_by_cadence(signals, strategy_state, run_date, force_all=...)` gains a `force_all` flag. The orchestrator sets it when Redis key `regime:transition:fresh` is present (set by `regime_liquidator` immediately after a flatten; TTL 24h).

## TradeJohn narrowing

### Action set
```
keep   — order placed as sized (default; ≥ 90% of tickers)
cancel — order suppressed; reason: highly alarming news
```

The multiplier field is removed. The prompt is rewritten:

> Your **only** action is to cancel orders on tickers with highly alarming news. "Highly alarming" means: regulatory enforcement (SEC/DOJ/FTC), fraud allegation, bankruptcy filing, going-concern qualification, FDA rejection (for biotech), CEO/CFO sudden departure under negative circumstances, plant/data-center catastrophic failure, accounting restatement, going-private rumour with hostile counterparty. Do not cancel on: earnings beats or misses, analyst rating changes, ordinary product launches, executive shuffles, M&A speculation without confirmed bidder, sector-wide news, broad market moves. **Default to keep.** Vetoes should be < 5 % of tickers per cycle.

### Operational
- TradeJohn now runs on **every** regime (not just LOW_VOL / TRANSITIONING).
- Per-ticker scaling logic in `regime_blended_sizer._consolidate_path` is removed; the formula's output is final.
- `tradejohn_confirmer.confirm()` returns `{ticker: {action: 'keep'|'cancel', rationale}}`; cancels translate to dropped orders.

## Lambda parameter

### Storage
`pipeline_config` table grows one row:
```
key='position_sizing_lambda', value='2.0', description='daily notional deployed = lambda × NAV (range 0.10–3.50)'
```

### API
- `GET /api/config/lambda` → `{value: 2.0, min: 0.10, max: 3.50, updated_at: ...}`
- `PUT /api/config/lambda` body `{value: 1.5}` → validates ∈ [0.10, 3.50], persists, returns new value.

### Dashboard
Portfolio page header gains a small "Daily allocation: λ = 2.0× NAV" control — a horizontal slider with the current value displayed inline. Drag → debounced PUT. The next pipeline cycle picks up the new value (sizer reads on each invocation).

## Weekly live-Sharpe update

### Schedule
New systemd timer: `openclaw-weekly-strategy-weights.{service,timer}` — Sunday 06:00 ET (before Monday market open; after Saturday MasterMind review at 18:00 ET; gives ops time to inspect MasterMind recommendations before weights flip).

### Process (`src/agent/curators/weekly_live_sharpe.js`)
1. For each (strategy, regime) in `strategy_regime_backtests`:
   - Query `signal_performance` joined to `execution_signals` and `market_regime_history` to get all closed trades that occurred while regime = R.
   - Compute `live_sharpe(s, R) = mean(pnl_pct) / stddev(pnl_pct)` over those closed trades.
   - Compute `live_n(s, R)` = closed-trade count.
   - Compute `effective_sharpe(s, R)` via the sample-size weighted formula.
2. Persist all to `strategy_weights_by_regime` (insert new versioned row; old rows kept for audit).
3. Run the per-regime normalization → compute `w(s, R)` and `w_daily(s, R)` for the new period.
4. Run the auto-demotion gate.
5. Post a summary to `#general`:
   - Top 5 weight gains, top 5 weight losses
   - Any auto-demotions
   - Any strategies whose `live_sharpe` now diverges from `bt_sharpe` by > 1.0 (flagged for MasterMind attention).

### Coexistence with MasterMind
- MasterMind's Saturday `comprehensive-review` (18:00 ET) writes to `strategy_memos`.
- MasterMind's Saturday `position-recs` (19:00 ET) writes to `strategy_sizing_recommendations`.
- The Sunday 06:00 ET hardcoded cron uses **only** the trade-data-derived live Sharpe. It does **not** consult MasterMind memos or recommendations.
- MasterMind recommendations remain operator-driven: the operator reviews `#strategy-memos` + `#position-recommendations` and applies changes via existing tools (which trigger a `strategy_weights` rebuild).

## Schema

### `strategy_weights_by_regime` (new)
```sql
CREATE TABLE strategy_weights_by_regime (
  id                BIGSERIAL PRIMARY KEY,
  strategy_id       TEXT NOT NULL,
  regime_state      TEXT NOT NULL,
  cadence_days      INTEGER NOT NULL,
  bt_sharpe         NUMERIC,
  bt_n              INTEGER,
  live_sharpe       NUMERIC,
  live_n            INTEGER,
  effective_sharpe  NUMERIC NOT NULL,
  weight            NUMERIC NOT NULL,    -- w(s, R), sums to 1.0 within regime
  daily_weight      NUMERIC NOT NULL,    -- w(s, R) / cadence_days
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  trigger           TEXT NOT NULL,       -- 'weekly_cron' | 'lifecycle_change' | 'manual'
  is_current        BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX strategy_weights_current_idx
  ON strategy_weights_by_regime (strategy_id, regime_state)
  WHERE is_current;
```

Old rows stay (audit). New rebuild flips `is_current=FALSE` on prior rows for that (strategy, regime), inserts fresh row with `is_current=TRUE`.

### `pipeline_config` (existing, one new row)
Seeded by migration 091.

## Non-goals

- **Intraday rebalance.** Daily cycle only. Within-day moves are TradeJohn's news-cancel only.
- **Annualisation.** Sharpe stays trade-grain (μ/σ over closed trades). Ratios are within-strategy comparisons, not external benchmarks; √252 is irrelevant.
- **Per-strategy notional minima beyond the $25 portfolio-wide floor.**
- **Auto-promotion.** Live-Sharpe improvements DON'T auto-promote `candidate` → `live`. MasterMind + operator do that.

## Risk + rollback

- **Concentration risk on quiet days.** If only one strategy on one ticker is "currently signalling", that ticker absorbs `λ × NAV` after normalization, capped at 25 % of NAV by the per-ticker safety. Acceptable given 4× margin headroom + daily cycle cadence.
- **Auto-demotion runaway.** The Sunday cron could mass-demote if a regime change exposes many strategies to a new regime where they have only a few losing trades. Defense: demotion requires `effective_sharpe ≤ 0` across **all** eligible regimes; a strategy with one good regime and one bad one stays live (just excluded from the bad regime).
- **Lambda misuse.** Operator drags the slider to 3.5× during a CRISIS regime. Defense: dashboard slider tooltip warns when current regime is HIGH_VOL/CRISIS and λ > 1.5×. (Soft warning only; user is the operator.)
- **Rollback path:**
  1. Set env `OPENCLAW_REGIME_BLENDED_LIVE=0` → orchestrator falls back to deterministic_sizer.
  2. Revert `regime_blended_sizer.py` changes from git.
  3. New schema is additive (new table, new pipeline_config row); rollback is simply "stop reading them".

## Verification plan

1. **Math invariant probe:** new `tools/verify_sizing.js` — for each daily cycle, assert:
   - `|Σ position_usd - λ × NAV| < $1` (lambda invariant)
   - `max |position_usd(t)| ≤ 0.25 × NAV + $1` (per-ticker cap)
   - For every active strategy in the current regime, `w(s, R) > 0` AND `Σ_s w(s, R) = 1.0 ± 1e-6` (normalization)
   - Σ_s effective_sharpe(s, R) used as the denominator matches the table snapshot.
2. **Sharpe blend regression:** unit test on synthetic (bt_n=200, bt_sharpe=2.0; live_n=50, live_sharpe=1.0): expected `effective = 1.8` → assert.
3. **Auto-demotion test:** set up a `live` strategy with `effective_sharpe = -0.1` in its only eligible regime; run cron; assert state flips to `candidate` + Discord notice.
4. **Regime-transition force-fire:** test that on `regime:transition:fresh` Redis key set, cadence gate returns ALL signals (no filter applied).
5. **TradeJohn smoke:** synthetic news input "FDA REJECTS XYZ NEW DRUG" → confirmer returns `cancel`; synthetic "XYZ beats earnings by $0.02" → returns `keep`.
6. **Lambda end-to-end:** PUT lambda=0.5; trigger sizer; assert total invested ≈ 0.5 × NAV.
7. **Dashboard visual:** screenshot the lambda slider and the weights view via `tools/page-shot.js`.

## Defaults (sensible, user can override on review)

| Parameter | Default | Notes |
|---|---|---|
| `cadence_days('daily')`    | 1  | trading days |
| `cadence_days('weekly')`   | 5  | trading days (Mon–Fri) |
| `cadence_days('monthly')`  | 21 | trading days |
| λ default                  | 2.0 | matches current effective leverage |
| λ slider range             | 0.10 – 3.50 | inside PDT margin headroom |
| Per-ticker cap             | 25 % of NAV | preserved from current sizer |
| Minimum trade threshold    | $25 | drop tickers under this |
| Weekly update cadence      | Sunday 06:00 ET | between MasterMind Sat review and Monday open |
| TradeJohn cancel quota     | < 5 % of tickers per cycle (soft) | rubric in prompt |
| Auto-demotion threshold    | effective_sharpe ≤ 0 across **all** eligible regimes | per-regime exclusion otherwise |
