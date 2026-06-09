# √cadence-normalized stops / take-profits in the live sizer

**Date:** 2026-06-09
**Status:** Approved (operator) — ship gated + flip live, no dry-run
**Scope:** Live equity sizer only (`regime_blended_sizer._sharpe_cadence_path`)

## Problem

Each contributing strategy emits a native bracket (`entry`, `stop_loss`,
`target_1`, `target_2`) calibrated to **its own holding horizon** (cadence). When
multiple strategies corroborate on the same ticker, the live sizer combines those
brackets (tightest stop + capped-sum take-profit across factor blocks, via
`bracket_stacking.stacked_bracket`; or the single max-weight pick via
`_select_bracket`). But it combines **raw gaps of mixed horizons** — a 5-day
strategy's stop gap and a 1-day strategy's stop gap are not on the same scale, so
the corroborated bracket is horizon-inconsistent.

The portfolio's *weighting* already corrects for this: `strategy_weights._regime_weight`
computes `daily_weight = effective_sharpe / √cadence_days` (Sharpe scales as
σ·√T, so a T-day holder's per-cycle contribution is `w/√T`). The book is
re-evaluated and delta-rebalanced **daily** (EOD compute → T+1 MOC). Stops and
take-profits should be normalized to the **same single-day basis** so that
corroboration of stops/TPs is consistent with the daily weighting process.

## Solution

Normalize each contributing strategy's bracket **gap from entry** to a single-day
equivalent by shrinking it by `1/√cadence_days`, mirroring the weight scaling.
Entry is the fill anchor (not a gap) and is unchanged.

For entry `e`, level `x ∈ {stop, t1, t2}`, cadence `c` (floored at 1),
factor `f = 1/√c`:

```
x_daily = e + (x - e) * f
```

This is direction-agnostic (the signed gap shrinks, sign preserved) and is
**algebraically identical** to dividing each strategy's `stop_pct` / `tp_pct` by
`√c` before the combine:

```
(e - x_daily) / e = ((e - x) / e) * f          # verified
```

A daily strategy (`c = 1` → `f = 1`) is unchanged.

### Insertion point — candidate construction, NOT the combine functions

Normalize each bracket candidate's `stop` / `t1` / `t2` **as it is built**, in the
`ticker_meta[tkr]['brackets'].append({...})` block of `_sharpe_cadence_path`
(currently `regime_blended_sizer.py` ~line 1000–1008), behind a gate.

Consequences:

- `bracket_stacking.py` (tightest-stop + capped-sum-TP) and the legacy
  `_select_bracket` max-weight pick are **untouched** — they receive
  already-normalized absolute levels and run exactly as today. The operator's
  chosen combine logic (tightest stop, capped-linear-sum TP) is preserved; only
  its *inputs* change.
- Applies in **both** combine paths and at **any** corroboration count (a single
  contributing strategy included), because normalization happens before the fork.
- Daily-cadence strategies are byte-identical.

### Cadence source

`_sharpe_cadence_path` **already builds** `cadence_by_strat = {r['strategy_id']:
float(r['cadence_days']) for r in rows}` (currently ~line 881) from the
`strategy_weights.load_current(regime)` rows that also produced `daily_weight`.
Reuse it directly — no new map, no new query. This guarantees the stop
normalization uses the identical cadence value as the weight normalization, with
zero drift. Default to `1.0` (no-op) for any sid somehow absent from the map.

### Gating / rollout

- New env gate `OPENCLAW_STRATEGY_CADENCE_STOP_NORM`, read via the existing
  `_ortho_enabled('OPENCLAW_STRATEGY_CADENCE_STOP_NORM')` idiom (`== '1'`).
- **Default OFF → byte-identical** to today (candidates built with raw levels).
- Operator rollout: merge gated code, then flip the gate to `1` in prod `.env`,
  restart johnbot, verify healthy. No shadow / dry-run step (operator directive).

## What does NOT change

- **Backtest**: untouched. Re-backtests still order at native cadence with
  profitability-optimized stops.
- **Strategy recommendations**: Mastermind comprehensive-review / position-recs /
  backtest coupling — untouched.
- **Option path** (`_consolidate_option_orders`): out of scope (option contributors
  are partitioned out of `active` before this equity loop; no live option strategy).
- **Weight / sizing math**: unchanged. Only `stop` / `t1` / `t2` levels are touched.

## Components

1. **Pure helper** in `src/execution/bracket_stacking.py` (the bracket-math module,
   already pure and unit-tested):
   ```python
   def daily_normalized_levels(entry, stop, t1, t2, cadence_days):
       """Return (stop, t1, t2) with each finite gap-from-entry shrunk by
       1/sqrt(max(1, cadence_days)). A level that is None/non-finite, or any
       level when entry is None/non-finite/<=0, is returned unchanged."""
   ```
   - `c = max(1.0, float(cadence_days))`; `f = 1.0 / math.sqrt(c)`.
   - Reuses the module's existing `_finite` guard.
   - If `entry` is not finite or `<= 0`: return `(stop, t1, t2)` unchanged.
   - Else for each level: `e + (x - e) * f` when finite, else pass through.

2. **Sizer wiring** in `src/execution/regime_blended_sizer.py`
   (`_sharpe_cadence_path`):
   - `cadence_by_strat` already exists (~line 881) — reuse it.
   - With the other gate reads (~line 886, next to `_size_scalar_on`): add
     `_cadence_stop_norm_on = _ortho_enabled('OPENCLAW_STRATEGY_CADENCE_STOP_NORM')`.
   - In the candidate-construction block (~line 1000): read raw `entry_price`,
     `stop_loss`, `target_1`, `target_2` from the signal; when
     `_cadence_stop_norm_on`, recompute `(stop, t1, t2) =
     bracket_stacking.daily_normalized_levels(entry, stop, t1, t2,
     cadence_by_strat.get(sid, 1.0))`; store entry (always raw) + (possibly
     normalized) stop/t1/t2 in the candidate dict.

## Consequence flagged & accepted

√cadence normalization sharply tightens long-horizon stops (a monthly 10% stop →
~2.2% after ÷√21), and the combine takes the **tightest** (min) stop across reps,
so corroborated stops get tighter. On a daily-rebalanced book this means more
intraday stop-outs followed by next-cycle re-entry, which can consume day-trading
buying power (cf. the documented DTBP-guard incident). **Operator decision: ship
WITHOUT a stop floor first** (cleanest, symmetric with the weight scaling); a
min-stop floor can be added later as a fast-follow only if live behavior shows
pathologically tight combined stops.

## Testing

- **Unit (pure helper, `tests/test_bracket_stacking.py` or a sibling):**
  - long & short: `stop_pct`/`tp_pct` scale by exactly `1/√c`.
  - `c = 1` → levels unchanged (no-op).
  - `c = 21` (monthly) → gap shrinks by `1/√21`.
  - `entry` None / non-finite / `<= 0` → all levels unchanged.
  - any level None / NaN → that level passes through; others still normalize.
- **Sizer-level (`tests/test_regime_blended_sizer_live.py` or sibling):**
  - gate OFF → emitted `stop`/`t1`/`t2` byte-identical to pre-change.
  - gate ON, multi-strategy ticker with mixed cadence → each candidate's
    `stop_pct`/`tp_pct` equals `raw/√c`, and the combined `min`-stop / capped-sum-TP
    is computed on the normalized inputs.
- **Regression:** existing `test_bracket_stacking*` and `test_regime_blended_sizer*`
  stay green (combine functions unchanged).
```