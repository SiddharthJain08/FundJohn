# Regime-Partitioned Backtest Backfill — Spec

**Status:** draft, pending sign-off.
**Trigger:** 2026-05-12 Phase 3 gate audit found gate B (2y walk-forward Sharpe/DD delta) unsatisfiable because `signal_pnl` has only 27 days of closed trades. Path 3 chosen by operator: backfill a per-strategy per-regime backtest table and re-point the walk-forward harness at it.

**Scope deliberately limited to gate B.** Gate A (30 trading days clean parity in HIGH_VOL/CRISIS regimes) is still market-condition-bound. Not addressed here; revisit when parity has accumulated real data.

---

## What we already have

- `data/master/historical_regimes.parquet` — 2523 trading days, 2016-04-11 → 2026-04-22. Counts: TRANSITIONING 1047, LOW_VOL 915, HIGH_VOL 411, CRISIS 150. Plenty of statistical power per regime.
- `src/strategies/auto_backtest.py:run_backtest(filepath)` — already partitions by regime. Returns `regime_breakdown[regime] = {sharpe, max_dd, total_return_pct, trade_count, oos_days, window_count}` for the four canonical regimes. No persistence today.
- `src/strategies/manifest.json` — 53 live + 7 staging + 33 candidate strategies. Each declares `eligible_regimes` (Task 11 backfilled this).
- `src/backtest/regime_blended_backtest.py` — current walk-forward harness reads `signal_pnl` (27 days). To be re-pointed.

## Design decisions

### 1 — New table, not extension of `backtest_results`

`backtest_results` is hypothesis-keyed (`hypothesis_id UUID`), used by StrategyCoder/research flows. Reusing it would conflate two different data sources. New table.

### 2 — Per-regime breakdown is canonical, not aggregate

`auto_backtest`'s `regime_breakdown` is what we want. Aggregate Sharpe across all regimes is misleading (a strategy can have great LOW_VOL Sharpe and bad HIGH_VOL Sharpe; the average hides the eligibility decision). One row per `(strategy_id, regime_state)`.

### 3 — Snapshot per `run_id`, not in-place upsert

Backtest results drift as strategy code changes. Each refresh writes a new snapshot under a new `run_id`. The harness reads the latest `run_id` only. History is retained (append-only — consistent with the master-data invariant).

### 4 — Harness simulates portfolio, not just trade deltas

The walk-forward question is: "Over the 10y window, would the blended sizer's portfolio have outperformed the production sizer's portfolio?" The blended sizer's effect on a strategy's contribution is:

- **Regime gate** — zero out the strategy in non-eligible regimes (already in `manifest.eligible_regimes`).
- **Cadence gate** — haircut fire-frequency. Implemented as a multiplicative factor on `trade_count` per regime (LOW_VOL/TRANSITIONING strategies fire less, since consolidation collapses correlated signals).
- **Consolidation** — in LOW_VOL/TRANSITIONING, multiple strategies firing on the same ticker collapse to one order. Cannot be derived from per-strategy backtests alone; requires a correlation/overlap assumption.

**Decision:** for v1, harness compares the *strategy-portfolio Sharpe/DD* under two filters:
- **Production proxy:** weighted sum of per-regime per-strategy returns, regime-weighted by `historical_regimes.parquet` day counts (i.e., what would a portfolio that ran every live strategy with current sizing have done).
- **Blended proxy:** same, but each strategy zeroed in regimes outside its `eligible_regimes`. Cadence/consolidation effects deferred to v2.

This is a *conservative* gate: it captures the regime-eligibility filter only. If even this gate fails (regime filter loses money), the blended sizer is a bad idea. If it passes, we proceed to live parity which captures the consolidation effect in real money.

### 5 — Weekly refresh, Sunday 06:00 ET

After Saturday's strategy review and before Monday's open. Single systemd timer; runs `backfill_regime_backtests.py` against current live + staging strategies.

---

## Migration 073

```sql
-- src/database/migrations/073_strategy_regime_backtests.sql
CREATE TABLE IF NOT EXISTS strategy_regime_backtests (
    run_id            UUID        NOT NULL,
    strategy_id       TEXT        NOT NULL,
    regime_state      TEXT        NOT NULL,  -- LOW_VOL | TRANSITIONING | HIGH_VOL | CRISIS
    sharpe            NUMERIC,
    max_dd            NUMERIC,
    total_return_pct  NUMERIC,
    trade_count       INTEGER,
    oos_days          INTEGER,
    window_count      INTEGER,
    note              TEXT,        -- 'not_declared' | 'no_oos_window' | NULL
    declared_regimes  TEXT[],
    period_start      DATE,
    period_end        DATE,
    run_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, strategy_id, regime_state)
);
CREATE INDEX IF NOT EXISTS idx_strb_strategy_regime
    ON strategy_regime_backtests (strategy_id, regime_state, run_at DESC);
CREATE INDEX IF NOT EXISTS idx_strb_latest
    ON strategy_regime_backtests (run_at DESC);
```

## Phase A — Schema + runner (≤1 day)

1. Migration 073 above.
2. `scripts/backfill_regime_backtests.py`:
   - Reads `manifest.json`, filters to states in `{live, staging}` (configurable via `--states live,staging`).
   - For each strategy, resolves filepath, calls `auto_backtest.run_backtest(filepath)`.
   - Unpacks `result['regime_breakdown']` → one INSERT per regime under a fresh `run_id`.
   - Logs per-strategy outcome to `logs/regime_backfill_<date>.log`.
   - `--dry-run` flag prints SQL without executing.
   - Idempotent on `(run_id, strategy_id, regime_state)`; safe to re-run a partial backfill if it crashes mid-way.
3. Tests in `tests/test_backfill_regime_backtests.py`:
   - Fixture: mock `auto_backtest.run_backtest` to return a known `regime_breakdown` dict; assert rows land correctly.
   - Edge cases: `note='not_declared'`, `note='no_oos_window'`, regime with zero trades.

## Phase B — First production run (~1-3h depending on per-strategy backtest cost)

1. Apply migration 073.
2. Run `python3 scripts/backfill_regime_backtests.py --states live` → 53 strategies × per-regime rows ≈ 200 rows.
3. Spot-check: SELECT for 3 strategies, compare against running `auto_backtest.py <file>` standalone — should match.

## Phase C — Harness rewrite (~1-2 days)

1. `src/backtest/regime_blended_backtest.py`:
   - Replace `signal_pnl` read with `SELECT … FROM strategy_regime_backtests WHERE run_id = (latest)`.
   - Replace per-trade simulation with the v1 portfolio model (regime-eligibility filter only, weighted by `historical_regimes` day counts).
   - Output schema stays the same (`output/regime_blended_walkforward.json` with `blended`, `production`, `delta`, `mode_distribution`).
   - `mode_distribution` is now the 10y historical distribution, not the 27-day signal_pnl distribution.
2. `tests/test_regime_blended_backtest.py`:
   - Existing tests rewritten against the new portfolio model.
   - Add: known-input regression test (fixture `strategy_regime_backtests` rows → known `delta` output).

## Phase D — Weekly cron (~1h)

1. `docs/strategy-backtest-refresh.service` + `.timer`:
   - `OnCalendar=Sun 06:00 America/New_York`.
   - `ExecStart=/usr/bin/python3 /root/openclaw/scripts/backfill_regime_backtests.py --states live,staging`.
2. Register under `/etc/systemd/system/`, `systemctl enable --now`.
3. Add row to `CLAUDE.md` infrastructure section.

## Phase E — Re-evaluate gate B (immediate after C)

1. Run the rewritten harness against the first backfill run.
2. Inspect `delta.sharpe` and `delta.max_dd`.
3. Outcomes:
   - **Both positive** → gate B passes; rollout proceeds on parity-only timeline (gate A still pending).
   - **Sharpe negative or DD worse** → blended sizer's regime-eligibility filter destroys value; rollback the rollout, escalate.
   - **Mixed** → operator call.

## Out of scope (parked)

- **Cadence/consolidation in the harness portfolio model.** Defer to v2 once gate B passes — these need either a per-day signal-overlap simulation or a tagged subset of `execution_signals` to be meaningful.
- **Gate A redesign.** Market-state-bound, separate decision.
- **Per-strategy backtest cost optimization.** If 53× `run_backtest` takes more than ~3h on the VPS, parallelize via Python multiprocessing. Don't optimize speculatively.

## Definition of done

- Migration 073 applied, `strategy_regime_backtests` populated with ≥1 run.
- Harness produces `output/regime_blended_walkforward.json` from the new source.
- Phase 3 gate B decision documented in `docs/superpowers/specs/2026-05-12-regime-backtest-backfill-spec.md` (this file, "Phase E outcome" appended).
- Weekly cron registered + verified by waiting for next Sunday's fire OR running manually with the systemd unit.

## Effort estimate

| Phase | Time | Owner |
|---|---|---|
| A — schema + runner | half day | BotJohn (direct) |
| B — first run | 1-3h (mostly wall-clock for backtests) | BotJohn (direct) |
| C — harness rewrite | 1-2 days | BotJohn (direct) — could delegate test rewrite to StrategyCoder |
| D — cron | 1h | BotJohn (direct) |
| E — gate decision | hours | operator decision |

Total: 3-4 days end to end. Parity window keeps collecting data concurrently — no schedule conflict.

---

## Phase E outcome (2026-05-12, same-day completion)

Ran the rewritten harness against the first backfill (`run_id=fb623c15-c289-48e6-b070-6a31035254f5`, 53 live strategies, 212 rows).

### Headline

```
delta.sharpe   = -0.1324  (blended 2.3636 vs production 2.4960)
delta.max_dd   =  0.0000  (both tied at 0.0539 — same worst-regime DD)
gate_b.pass    = false
```

**Gate B fails on Sharpe; max-DD is neutral.** ~5% relative Sharpe deterioration — far smaller than the original 27-day signal_pnl harness reported (-5.11 delta on a 12-scale = 42% deterioration), and now interpretable.

### Per-regime breakdown (10y historical day weights: LOW_VOL 36.3%, TRANSITIONING 41.5%, HIGH_VOL 16.3%, CRISIS 6.0%)

| Regime | Production strategies | Blended strategies | Production Sharpe | Blended Sharpe | Source of delta |
|---|---|---|---|---|---|
| LOW_VOL | 21 | 16 | 3.37 | 3.01 | **5 strategies filtered** — primary contributor |
| TRANSITIONING | 40 | 40 | 3.31 | 3.31 | identical (all strategies eligible) |
| HIGH_VOL | 15 | 13 | 0.00 | 0.00 | 2 strategies filtered, balanced out |
| CRISIS | 4 | 4 | -1.70 | -1.70 | identical (no filter applies) |

### Root cause

The 5 strategies the eligibility filter drops from LOW_VOL — all momentum-family — have `manifest.eligible_regimes = ['TRANSITIONING']` but their `auto_backtest` regime breakdown shows they earned **Sharpe 2.34 to 7.08** during historical LOW_VOL periods on 70-140 trades each:

| Strategy | LOW_VOL Sharpe | Trades | manifest eligible_regimes |
|---|---|---|---|
| `momentum_12_1` | 7.08 | 140 | `['TRANSITIONING']` |
| `S9_dual_momentum` | 6.23 | 70 | `['TRANSITIONING']` |
| `S_custom_jt_momentum_12mo` | 5.86 | 70 | `['TRANSITIONING']` |
| `S25_dual_momentum` | 2.34 | 119 | `['TRANSITIONING']` |
| `S25_dual_momentum_v2` | 2.34 | 119 | `['TRANSITIONING']` |

Trade-weighted Sharpe of the dropped subset: **4.62** vs production LOW_VOL average 3.37. Filtering them removes value from the blended portfolio.

### Interpretation — three possibilities, can't decide without research input

1. **Phase 1 backfill was too narrow.** Task 11's heuristic set eligibility based on something other than raw backtest Sharpe (e.g. fire-frequency thresholds, or filtered on production live history alone). The momentum strategies' LOW_VOL Sharpe shows they should be eligible for LOW_VOL.
2. **Deliberate research call.** Researchers/operator chose `TRANSITIONING`-only for these knowing production costs/slippage make LOW_VOL unprofitable despite price-only backtest Sharpe — backfill respected that.
3. **Harness portfolio model is too generous to production.** Equal-weight compounding across 21 LOW_VOL strategies overstates production return (49.79%) — real production sizer doesn't equal-weight; it Kelly-sizes. A realistic production model might show smaller delta.

### Three paths forward

| Path | Action | Risk |
|---|---|---|
| **A** — Re-tune the 5 manifests | Manually set `eligible_regimes` to include LOW_VOL for the 5 momentum strategies; re-run harness. If it then passes, gate B is satisfied. | Bypasses the original research decision (possibility 2). Should be a researcher's call, not an auto-fix. |
| **B** — Accept fail, redesign gate B | Treat 5% Sharpe deterioration as within model error; gate B passes if delta.sharpe > -X% on a documented threshold. | Loosens the gate; needs Phase 3 spec amendment. |
| **C** — Investigate before deciding | Open `dual_momentum` / `momentum_12_1` and check whether the strategy's `eligible_regimes` is hardcoded in source vs only in manifest; ask researcher who set Phase 1 eligibility why TRANSITIONING-only; check whether those strategies have actually been firing in LOW_VOL in production lately. | Slower; right answer if you don't want to silently flip eligibility. |

### Operator decision pending

This is a research/portfolio call, not an engineering bug to fix autonomously. Surface to user for path A/B/C selection.

### What's now durable

- Migration 073 applied; `strategy_regime_backtests` populated with 212 rows under `run_id=fb623c15-c289-48e6-b070-6a31035254f5`.
- `src/backtest/regime_blended_backtest.py` rewritten against the new source; 7/7 unit tests pass.
- `output/regime_blended_walkforward.json` reflects the new gate-B model.
- `openclaw-strategy-backtest-refresh.timer` registered, next fire Sun 2026-05-17 10:00 UTC (06:00 ET).
- Harness output now carries `gate_b: {sharpe_positive, max_dd_not_worse, pass}` — operators read a single boolean.

### What's NOT addressed

- Gate A (clean parity in HIGH_VOL/CRISIS) remains structurally unsatisfiable in the 30-day window — separate decision.
- v2 portfolio model (cadence + consolidation effects) deferred — only needed if path B is chosen.

---

## Phase F — Path C/B hybrid decision (2026-05-12)

**Operator decision:** Path C/B hybrid.

### Phase C investigation findings

| Question | Finding |
|---|---|
| Did the 5 momentum strategies fire in LOW_VOL in production? | **Yes.** Per `execution_signals`: S9 (15), S_custom_jt (15), S25 (21), S25_v2 (21) all fired LOW_VOL across 2026-04-29 → 2026-05-01. momentum_12_1 fired only in TRANSITIONING. |
| Is `eligible_regimes=['TRANSITIONING']` enforced today? | **No.** The `regime_gate.is_eligible()` call is wired in, but in `git HEAD:src/strategies/manifest.json` none of the 5 strategies have `eligible_regimes` set. The constraint exists **only in uncommitted working-tree** — production has never seen it. |
| What does the filter cost / save per regime? | LOW_VOL: Sharpe 3.374 → 3.009 (cost 0.365). TRANSITIONING/HIGH_VOL/CRISIS: identical. Filter has **zero observable benefit** in extreme regimes in the 10y backtest. |

**Interpretation:** the eligibility filter as currently declared is a *prospective policy proposal*, not a retrospective restriction. Gate B's strict `delta.sharpe > 0` cannot be satisfied because the proposed filter is a sacrifice trade purchased for unseen-regime robustness, not a free aggregate-Sharpe win.

### Phase B amendment

`src/backtest/regime_blended_backtest.py` updated:

```python
SHARPE_REGRESSION_TOLERANCE_ABS = 0.25
```

New `gate_b` predicate:
- `sharpe_within_tolerance` = `delta.sharpe >= -0.25`
- `max_dd_not_worse` = `blended.max_dd <= production.max_dd`  ← hard
- `low_vol_strategies_present` = `blended.per_regime.LOW_VOL.strategy_count >= 1` (sanity)
- `pass` = all three

`sharpe_positive` retained as informational field. Old strict-positive semantics are no longer the gate.

**Rationale for 0.25 absolute tolerance.** Empirically the filter costs 0.13 Sharpe today. 0.25 is roughly 2× the observed cost — fails on a doubling of harm but accommodates the current proposal. Threshold is documented at-source (constant with docstring); future revisits should land in spec.

**Note on Sharpe scale:** auto_backtest annualizes per-trade σ which inflates absolute Sharpe numbers. Both `production` and `blended` paths run through the same scale so the *delta* is comparable; the tolerance constant lives in delta-space.

### Final gate B result (2026-05-12 run)

```
delta.sharpe                     = -0.1324
delta.max_dd                     =  0.0000
sharpe_within_tolerance          = true   (|-0.1324| < 0.25)
max_dd_not_worse                 = true   (tied at 0.0539)
low_vol_strategies_present       = true   (16 strategies in blended LOW_VOL)
gate_b.pass                      = TRUE
```

### Tests

`tests/test_regime_blended_backtest.py` — 10/10 passing:
- 7 pre-existing eligibility / aggregation tests
- 3 new: tolerance constant sanity, end-to-end pass within tolerance, DD-not-worse guard evaluated

### Caveats — what Gate B does and does not authorize

**Scope mismatch flag.** `OPENCLAW_REGIME_BLENDED_LIVE=1` (the Phase 3 cutover flag) swaps the **trade step** from `trade_agent_llm` to `regime_blended_sizer_live` — i.e., it turns on **regime-conditional position sizing scalars**. It does **not** turn on the eligibility filter — `engine.run_strategies()` enforces `regime_gate.is_eligible()` independently in the **signals step**, which runs in *both* DRY-RUN and LIVE pipelines. Gate B as constructed validates the eligibility filter's portfolio effect; it does **not** validate the sizing scalars. A passing Gate B clears a Phase 3 *prerequisite* for the LIVE flip, but is not by itself the sizer validation. The sizer validation lives in Phase 2 DRY-RUN parity (Gate A).

**DD guard is structural under current aggregation.** `portfolio_per_regime` computes per-regime max_dd as the worst single strategy's max_dd. The eligibility filter only removes rows, so the surviving subset's max-of can only decrease or stay tied. Aggregation across regimes (max-of-per-regime) preserves this monotonicity. Therefore `delta.max_dd <= 0` is guaranteed by construction. The guard is kept in the gate predicate as a contract (and would constrain a future portfolio model with compounded DD), but is **not** what's keeping Gate B from failing today. The only constraint actually doing work is the Sharpe tolerance.

### Open follow-ups (still NOT addressed in this phase)

1. **Manifest eligibility status:** `eligible_regimes` for the 5 momentum strategies remains uncommitted in `src/strategies/manifest.json`. Three options for the operator:
   - (a) Commit it — `regime_gate.is_eligible()` will start filtering them out in LOW_VOL across both DRY-RUN and LIVE pipelines. Expect signal-volume drop for these names. Aligns production behavior with Gate B's tested scenario.
   - (b) Revert it — keep current production behavior (these strategies fire in LOW_VOL). Gate B becomes a hypothetical-policy validation only.
   - (c) Hold uncommitted as a Phase 1 backfill staging artifact pending researcher review of whether `['TRANSITIONING']` was the intended call.

   **Recommended:** decide before flipping `OPENCLAW_REGIME_BLENDED_LIVE=1`, otherwise the LIVE production behavior depends on whatever happens to be in the working tree.
2. **Gate A:** 30-day clean parity in HIGH_VOL/CRISIS still structurally unsatisfiable inside the 30-day window. Revisit when parity data accumulates or regime conditions shift.
3. **Sharpe scale revisit:** if auto_backtest σ-annualization is fixed downstream, the 0.25 tolerance constant needs proportional adjustment.
4. **Harness DD model upgrade (optional):** replacing worst-strategy-per-regime DD with compounded portfolio DD would make the `max_dd_not_worse` guard constraining. Not needed for the current Path C/B decision.
