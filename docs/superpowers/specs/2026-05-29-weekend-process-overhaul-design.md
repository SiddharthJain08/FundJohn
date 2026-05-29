# Weekend Process Overhaul — Design

**Date:** 2026-05-29
**Branch:** `feat/weekend-process-overhaul` (worktree off `feat/dashboard-backtest-metrics` / PR #13)
**Status:** Approved (design); operator authorized flipping the gate live on completion.

---

## Goal

Reorganise the weekend so that (1) per-strategy stop-loss / take-profit recommendations are **directly backtested and auto-applied when they improve Sharpe**, (2) the dashboard is refreshed with backtest metrics every weekend, (3) weekend jobs run on a clean four-slot schedule, (4) data-pipeline notifications consolidate into `#data-alerts`, and (5) the operator approvals page becomes a "Strategy Adjustments" view showing the week's applied changes alongside the existing weight/eligibility proposals.

## Background — what exists today (grounded in code)

- **Two disconnected recommendation systems.**
  - `strategy_sizing_recommendations` (written Sat 19:00 ET by `src/agent/curators/position_recommender.js` from `strategy_memos`): fields `size_delta_pct`, `stop_delta_pct`, `target_delta_pct`, `hold_days_delta`. Consumed by `src/execution/trade_handoff_builder.py` — **the stop/TP deltas are read into the Monday handoff but never applied to any signal.** Latent no-op.
  - `strategy_regime_params` (migration 076) + `strategy_regime_param_proposals` (mig 078) + audit `strategy_regime_param_changes` (mig 077): per-(strategy, regime) overrides `eligible`, `size_scalar`, `stop_pct`, `target_pct`, `max_hold_days`. The dashboard "Pending Regime Proposals" page approves these via `src/strategies/proposal_manager.py` → `src/strategies/eligibility_manager.py:set_params(...)`.
- **The override read-side is 90% built but orphaned.** `src/execution/regime_param_resolver.py` exposes `is_eligible`, `size_scalar` (multiplier, default 1.0), `stop_pct_override` / `target_pct_override` (absolute fraction, e.g. `0.05` = 5%), `max_hold_days_override`. Only `is_eligible` and `size_scalar` are actually read live (in `trade_handoff_builder.py`). **`stop_pct_override` / `target_pct_override` are never called by anything** — neither live execution nor `unified_backtest`. The columns are NULL for every strategy today.
- **Backtest** (`src/backtest/unified_backtest.py:simulate_trade`, ~L553-556) reads `sig.stop_loss` / `sig.target_1` straight off each emitted `Signal`; no override, no scaling.
- **Backtest refresh** (`src/maintenance/refresh_backtests.sh`): `unified_backtest --all-live` (auto-rebuilds `strategy_backtest_panel` per strategy) then `eligibility_assigner --all`. Scheduled Sat 06:00 UTC.
- **Weekly strategy weights** (`src/agent/curators/weekly_live_sharpe.js`): rebuilds `strategy_weights_by_regime` (`weight = effective_sharpe`; OUE multiplier removed 2026-05-29). Scheduled Sun 06:00 ET.
- **Discord routing.** Ingestion summaries (SOD collector end + EOD refresh) already post to `#data-alerts` via `src/pipeline/run_collector_once.js`. The orchestrator's per-step ▶️/✅/❌ boundary posts (`src/execution/pipeline_orchestrator.py`, all 11 steps) go to `#pipeline-feed`. Step-failure posts route to `#data-alerts`/`#trade-reports` via `STEP_FAILURE_CHANNEL`.
- **Approvals page.** Strategies tab → `#rp-section` "📋 Pending Regime Proposals", rendered by `_rpRender()` in `src/channels/api/server.js` (~L8292-8338); data via `GET /api/regime-proposals?status=pending` (`src/channels/api/routes_regime_proposals.js`).

## Canonical regimes

`LOW_VOL`, `TRANSITIONING`, `HIGH_VOL`, `CRISIS` (`eligibility_manager.CANONICAL_REGIMES`).

## Master gate

A single default-OFF env gate **`OPENCLAW_BACKTEST_COUPLED_RECS`** controls the entire loop:
- the override **read-side** in both backtest and live execution, AND
- the Saturday **auto-apply write**.

When OFF: no overrides are read and none are written → byte-identical to today (columns are NULL, getters return None, callers fall through to signal-level stops). When ON: backtest and live honor the same per-(strategy, regime) override, and the Saturday driver writes winners. Tying read + write to one gate guarantees the Sharpe comparison stays valid (backtest and live can never diverge on whether overrides apply).

---

## Workstream A — Couple recommendations ↔ backtest (core)

### A1. Wire the override read-side (gated)

Add override application at the two points where stops/targets are consumed, both keyed on the regime in effect and both no-ops unless `OPENCLAW_BACKTEST_COUPLED_RECS=1`:

1. **Backtest** — `unified_backtest.simulate_trade` (or its caller `_per_bar_simulate`): for each signal, look up the regime for that signal's date (the backtest is already regime-partitioned via `historical_regimes`), resolve the override (see source below). When non-None, replace the signal's stop/target with `entry_price * (1 ± override)` (direction-aware, mirroring the existing default-stop formula at L553-556). When None, keep `sig.stop_loss` / `sig.target_1`.
2. **Live** — `src/execution/engine.py` where `execution_signals` rows are built (the per-strategy regime is already selected there, per the SP-3.1 crypto work). Apply the same override to `stop_loss` / `target_1` before the row is persisted, so the handoff, executor, OCO reattach, and reports all see one consistent value.

**Override source.** `run_backtest` gains an optional `param_override` kwarg:
- `param_override=None` (default; live execution always uses this path) → resolve via `regime_param_resolver.stop_pct_override` / `target_pct_override` reading the **persisted** `strategy_regime_params` (gated; None when the gate is OFF or no row exists).
- `param_override={regime: {'stop_pct': x, 'target_pct': y}}` (or a callable `(strategy_id, regime) -> (stop_pct, target_pct)`) → use the injected map and **ignore the resolver/DB entirely**. This lets the coupling step test a candidate override **without writing the DB** (no write-then-rollback). 

**Semantics:** override is **absolute-replace** (a flat stop/target % for that strategy in that regime), matching `size_scalar`'s established precedent and the resolver's existing getter contract. Helper `apply_regime_param_override(strategy_id, regime, entry_price, direction, stop, target, *, override_source) -> (stop, target)` is the single shared implementation imported by both call sites (DRY), where `override_source` is either the resolver (live + baseline) or the injected map (candidate).

### A2. The coupling step (`src/execution/backtest_coupled_recs.py`)

Runs inside the Saturday driver (workstream C), after `position-recs` has written `strategy_sizing_recommendations`. Gated by `OPENCLAW_BACKTEST_COUPLED_RECS`.

For each strategy that has a fresh recommendation with a **non-zero `stop_delta_pct` and/or `target_delta_pct`** (skip strategies with no stop/TP rec — bounds the backtest count):

1. **Baseline backtest** — fresh `run_backtest(strategy_id, commit=False, param_override=None)` → resolves the strategy's **current persisted** overrides (None for everyone on the first run; whatever was applied in a prior week thereafter). This is true "current live behaviour". Record `baseline_sharpe` (the run's `total_sharpe`) and `baseline_n_trades`. Capture the strategy's median signal-level stop distance and target distance over the window (for the candidate transform).
2. **Derive candidate absolute params.** `candidate_stop_pct = clamp(baseline_median_stop_pct * (1 + stop_delta_pct/100), 0.01, 0.30)`; same for target. *(The exact reading of `stop_delta_pct` — multiplicative % change vs absolute percentage-points — must be confirmed against `position_recommender.js:_deriveDeltas`/`comprehensive_review.js` at implementation time; this spec assumes multiplicative % change and the implementer pins it down with a test.)*
3. **Candidate backtest** — `run_backtest(strategy_id, commit=False, param_override={R: {'stop_pct': candidate_stop_pct, 'target_pct': candidate_target_pct} for R in eligible_regimes})`. Injected map, **no DB write**. Record `candidate_sharpe`, `candidate_n_trades`.
4. **Accept rule:** apply **iff** `candidate_sharpe - baseline_sharpe >= 0.10` **AND** `candidate_n_trades >= 30`.
5. **Apply (winners only):** for **every regime the strategy is eligible in**, call `eligibility_manager.set_params(strategy_id=..., regime_state=R, stop_pct=candidate_stop_pct, target_pct=candidate_target_pct, actor='saturday_coupling', reason=<one-line incl. ΔSharpe>, source='saturday_coupling', bt_sharpe_before=baseline_sharpe, bt_sharpe_after=candidate_sharpe, bt_n_trades=candidate_n_trades)`. This upserts `strategy_regime_params` and writes one `strategy_regime_param_changes` audit row per regime. Mark the source `strategy_sizing_recommendations.action_taken='applied'`; losers → `'ignored'` with the ΔSharpe in `reasoning`.

"Eligible regimes" = the strategy's current `eligible_regimes` (same source the sizer reads). If a strategy has none seeded, treat all four canonical regimes as eligible (matches resolver backward-compat).

### A3. Schema (migration 125, additive)

`strategy_regime_param_changes` gains three nullable columns: `bt_sharpe_before NUMERIC`, `bt_sharpe_after NUMERIC`, `bt_n_trades INT`. `eligibility_manager.set_params` accepts three optional kwargs and writes them into the audit insert (silent-strip-safe: existing callers omit them → NULL). No change to `strategy_regime_params`.

---

## Workstream B — Weekly dashboard backtest refresh

The Saturday driver, after the full backtest refresh (which already rebuilds `strategy_backtest_panel` inline per strategy via `unified_backtest`), runs an explicit **panel-rebuild sweep** for all live/candidate strategies and a verification check that each has a panel `computed_at` newer than its latest backtest run. Net effect: every Saturday the dashboard reflects refreshed backtest metrics **including the week's applied stop/TP overrides** (because the full refresh runs *after* the coupling apply). No new dashboard read path — the existing `/api/strategies` and `/api/strategies/:id/backtest-curve` already read the panel.

---

## Workstream C — New weekend schedule (the reorg)

One sequenced Saturday-morning driver replaces the scattered Sat 16:00/18:00/18:30/19:00/20:00 + Sun 06:00 timers. Research moves to Sunday. Maintenance runs 20:00 ET both days.

| When (ET) | systemd unit | Contents (in order) |
|---|---|---|
| **Sat 08:00** | `openclaw-weekend-saturday` (new) → `src/maintenance/weekend_saturday.sh` | 1. comprehensive-review (`run_mastermind.js --mode comprehensive-review`) → `strategy_memos`<br>2. critique fan-out (`--mode critique`) — review diligence<br>3. position-recs (`--mode position-recs`) → `strategy_sizing_recommendations`<br>4. **coupling (A2)** → applies winning stop/TP overrides (gated)<br>5. full backtest refresh (`refresh_backtests.sh`: `unified_backtest --all-live` + `eligibility_assigner --all`) — now reflects applied overrides<br>6. weekly strategy-weights (`weekly_live_sharpe.js`)<br>7. **panel rebuild sweep + verify (B)**<br>8. universe-recs (`--mode universe-recs`, gated `OPENCLAW_UNIVERSE_RECS` as today)<br>9. post "Strategy Adjustments" summary to `#position-recommendations` |
| **Sat 20:00** | `openclaw-weekend-maintenance-sat` (new) → `run_maintenance.js --mode weekend-sat` | system health + audit that the morning pipeline succeeded (memos written, recs written, coupling ran, backtests/weights/panels fresh). |
| **Sun 08:00** | `openclaw-weekend-sunday` (new) → `run_mastermind.js --mode saturday-brain` | the 8-phase research run (internally unchanged; only the timer moves). |
| **Sun 20:00** | `openclaw-weekend-maintenance-sun` (new) → `run_maintenance.js --mode weekend-sun` | `After=openclaw-weekend-sunday` → audit + surgical recovery of the research run (the role today's `--mode saturday` plays, now correctly *after* research). |

**Step sequencing** within `weekend_saturday.sh`: each step runs to completion before the next; a non-zero exit on steps 1–4 (review/critique/recs/coupling) logs a WARN and continues (a missing memo shouldn't block the backtest refresh), but step 5 (backtest refresh) failing aborts steps 6–7 (weights/panels would be stale). All steps `tee` to a dated log under `/var/log/openclaw/` (matching `refresh_backtests.sh`).

**`run_maintenance.js` modes:** add `weekend-sat` and `weekend-sun`; keep `daily`. Remap the existing `saturday` (research audit) behaviour to `weekend-sun`; `weekend-sat` is the new pipeline-audit mode. The read-only `saturday-verify` mode is retired (its role folds into `weekend-sun`).

**Decommissioned (disable, do not delete — documented):** `openclaw-mastermind-corpus`, `openclaw-paper-expansion` (both already superseded by `saturday-brain`), `openclaw-backtest-refresh`, `openclaw-strategy-backtest-refresh`, `openclaw-weekly-strategy-weights`, `openclaw-strategy-review`, `openclaw-mastermind-critique`, `openclaw-position-recs`, `openclaw-universe-recs`, `openclaw-botjohn-saturday-maintenance`, `openclaw-botjohn-saturday-verify` — their work is folded into the four new units. `systemctl disable --now` each, with a note in `docs/`. Weekday timers (maintenance Mon–Fri, EOD, premarket, stop-reattach, regime, options-archive, etc.) are untouched.

**Timezone:** all four new timers use `OnCalendar=Sat/Sun *-*-* 08:00:00 America/New_York` (and `20:00:00`), matching the explicit-ET convention used by most existing units.

---

## Workstream D — Discord routing consolidation

- **`src/execution/pipeline_orchestrator.py`:** all per-step ▶️/✅/❌ boundary posts route to **`#data-alerts`** instead of `#pipeline-feed`. `#pipeline-feed` keeps only the **daily cycle bookend** — one "cycle started" post and one "cycle completed (summary)" post. Implement via a small routing constant (e.g. `STEP_BOUNDARY_CHANNEL = 'data-alerts'`, `CYCLE_BOOKEND_CHANNEL = 'pipeline-feed'`) so the split is one obvious place. Step-failure routing (`STEP_FAILURE_CHANNEL`) is unchanged.
- **SOD ingestion summary:** `src/pipeline/run_collector_once.js` — promote the start-of-day collector post to a **full per-phase ingestion summary** reusing the EOD formatter (`formatEodAlert`-style), posted to `#data-alerts`. Factor the EOD formatter into a shared `formatIngestionSummary(kind)` so SOD and EOD share one renderer (`kind ∈ {'SOD','EOD'}`). 

No webhook/registry changes — `#data-alerts` and `#pipeline-feed` are both BotJohn-owned already.

---

## Workstream E — "Strategy Adjustments" page

Rename the Strategies-tab `#rp-section` header from "📋 Pending Regime Proposals" to **"⚙️ Strategy Adjustments"** and split it into two parts:

1. **Applied this week** (new, read-only): rows from `strategy_regime_param_changes WHERE source='saturday_coupling' AND changed_at >= now() - interval '7 days'`, showing strategy, regime(s), `stop_pct`/`target_pct` before→after, and **ΔSharpe** (`bt_sharpe_after - bt_sharpe_before`) + `bt_n_trades`. New route `GET /api/strategy-adjustments/applied?days=7` in `routes_regime_proposals.js`.
2. **Pending proposals** (unchanged): the existing `strategy_regime_param_proposals` (size_scalar / eligibility / weight knobs) with the existing approve/reject/modify actions intact. This is the "proposals to adjust strategy weights as before."

Frontend: extend `_rpRender()` to render both sub-tables under the renamed header; the nav button label stays "Strategies" (the section lives inside that tab).

---

## Files

**Create**
- `src/execution/backtest_coupled_recs.py` — coupling step (A2).
- `src/execution/regime_param_override.py` — shared `apply_regime_param_override(...)` helper (A1), imported by backtest + engine.
- `src/database/migrations/125_param_change_backtest_cols.sql` — A3.
- `src/maintenance/weekend_saturday.sh` — Saturday driver (C).
- `docs/openclaw-weekend-saturday.{service,timer}`, `…-weekend-maintenance-sat.{service,timer}`, `…-weekend-sunday.{service,timer}`, `…-weekend-maintenance-sun.{service,timer}` — new units (C).
- `docs/weekend-schedule-migration.md` — what was disabled and why (C).
- Tests: `tests/test_regime_param_override.py`, `tests/test_backtest_coupled_recs.py`, `tests/test_param_change_audit_cols.py`, `tests/test_pipeline_feed_routing.py`, `tests/test_sod_ingestion_summary.py` (+ a dashboard render check).

**Modify**
- `src/backtest/unified_backtest.py` — call the override helper in the simulate path (A1, gated).
- `src/execution/engine.py` — call the override helper before writing `execution_signals` (A1, gated).
- `src/strategies/eligibility_manager.py` — `set_params` accepts `bt_sharpe_before/after`, `bt_n_trades` (A3).
- `src/agent/run_maintenance.js` — `weekend-sat` / `weekend-sun` modes (C).
- `src/execution/pipeline_orchestrator.py` — step-boundary → `#data-alerts`; bookend → `#pipeline-feed` (D).
- `src/pipeline/run_collector_once.js` — shared `formatIngestionSummary`, full SOD summary (D).
- `src/channels/api/routes_regime_proposals.js` — `GET /api/strategy-adjustments/applied` (E).
- `src/channels/api/server.js` — rename header, render "Applied this week" + pending (E).

## Gates & deploy

- `OPENCLAW_BACKTEST_COUPLED_RECS` — default-OFF; controls A1 read-side + A2 write. **Operator authorized flipping this to `1` on completion.** Safe to flip immediately: overrides are NULL until the first Saturday run writes them, so the first live effect is next Saturday's coupling.
- Existing `OPENCLAW_UNIVERSE_RECS` still gates step 7.
- Deploy: commit + push branch → run migration 125 → install/enable the four new timers + disable the superseded ones → flip the gate in VPS `.env` → restart `johnbot`. Dry-run `weekend_saturday.sh` (steps that accept `--dry-run`, or a one-strategy coupling dry-run) before relying on the live timer.

## Testing strategy (TDD)

- **A1:** override → simulate_trade exits at the overridden level (not the signal level); None override → byte-identical to today; live engine writes overridden stop into `execution_signals`. Gate OFF → no override read.
- **A2:** accept rule boundaries (ΔSharpe 0.099 vs 0.10; n_trades 29 vs 30); winner writes to all eligible regimes + audit rows with sharpe cols; loser marks `action_taken='ignored'`; strategies with no stop/TP rec are skipped.
- **A3:** new columns nullable; existing `set_params` callers unaffected.
- **D:** step-boundary posts target `data-alerts`; bookend targets `pipeline-feed`; SOD summary has the same per-phase shape as EOD.
- **E:** applied route returns the week's `saturday_coupling` changes; render shows both sub-tables.

## Out of scope (YAGNI)

- `size_scalar` and `max_hold_days` coupling — size is owned by `weight = effective_sharpe` (OUE multiplier removed last session); `max_hold_days` untouched. Only `stop_pct`/`target_pct` are coupled.
- No change to the research run's internals (only its timer moves).
- No master-data mutation.
