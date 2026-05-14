# Per-strategy alpha = Information Ratio (predictive value)

**Date:** 2026-05-14
**Status:** approved
**Target files:**
  - `src/channels/api/server.js` (`/api/portfolio/ticker-alpha/:ticker` payload + `_buildAlphaBarsHtml`)

## Problem statement

Two iterations on the alpha-bars metric so far have each missed:

1. **Sharpe decomposition** (Σ alpha = ticker live Sharpe) — math-pretty but didn't speak to "how informative were the strategy's predictions on this ticker?"
2. **Pct-return ratio** (alpha = strat_avg / ticker_avg) — easy to read, but
   - asymmetric visual: a strategy that runs against the ticker's positive avg gets `alpha < 0`, and the bar's `|alpha − 1|` distance dominates the visual scale, dwarfing strategies a bit above/below baseline
   - doesn't risk-adjust: a strategy with one lucky +50% trade and one −45% trade looks identical in ratio terms to a strategy that hit +2% every cycle

The operator's actual question: **how valuable were each strategy's PREDICTIONS in generating profit?** Predictive value, risk-adjusted, comparable across strategies, confidence-aware about sample size.

## The metric — Information Ratio (IR)

For each `(strategy, ticker)` pair where the strategy has ≥ 1 closed trade on the ticker:

```
ticker_baseline_pct = mean(pnl_pct across every closed trade on the ticker)

IR_raw(s, t) = (mean(strat_pnl_pct on t) − ticker_baseline_pct(t))
               / std(strat_pnl_pct on t)

IR(s, t)     = IR_raw(s, t) × √min(n_trades, 30) / √30
```

### Interpretation

| Value | Meaning |
|---|---|
| `IR > 1` | Predictions reliably beat baseline relative to their own variance. Strong, low-noise edge. |
| `IR > 0` | Strategy's predictions extract excess return over the ticker's baseline. Informative. |
| `IR = 0` | Predictions don't outperform the baseline (statistically indistinguishable from random ticker trades). |
| `IR < 0` | Predictions are destructive — the strategy's signals on this ticker produced worse outcomes than the ticker's overall mix. |

### Why this metric

- **Captures predictive value** — `mean − baseline` in the numerator is literally the strategy's excess return above the ticker's typical outcome. Bigger numerator = more informative signals.
- **Captures profit** — entirely realized PnL of closed trades; nothing simulated or projected.
- **Risk-adjusted** — `std` in the denominator means consistent edges beat lucky outliers.
- **Confidence-adjusted** — `√min(n, 30) / √30` shrinks low-`n` IRs toward zero so single-trade heroes don't dominate. At `n = 30+` the strategy gets full credit for its raw IR.
- **Symmetric in sign** — centered on zero. A strategy with `IR = +1.4` and one with `IR = −1.4` render as mirror-image bars.

### Edge cases

- **`std = 0`** (single closed trade, or all-identical pnls) → `IR = null`. Strategy renders as a muted "n/a" row sorted below the ranked bars, like today's pre-Sharpe-data fallback.
- **`n_trades = 0`** on this ticker → strategy not in the result set at all.
- **`ticker_n < 2`** → endpoint returns `reason: 'insufficient_days'` (existing fallback panel).

### Ticker baseline definition

`ticker_baseline_pct = AVG(pnl_pct)` across *every* closed trade on the ticker — including the strategy whose IR is being computed. We deliberately don't exclude the strategy's own trades from the baseline:

- Including-self makes the baseline a stable property of the ticker that doesn't depend on which strategy is being scored.
- The "excess return" interpretation stays clean: `mean_strat − ticker_avg` answers "did this strategy's average beat what the ticker as a whole did?".
- Excluding-self would inflate the IR for strategies with many trades on the ticker (subtracting their drag from the baseline they're scored against) — an unwanted self-flattering bias.

## Visual encoding

The asymmetric-bar bug in the current ratio rendering is gone by construction: IR is naturally centered on 0.

- Bars centered on the **zero line** of the track.
- Width: `|IR| / max(|IR| over strategies in this set) × 50%` of the track.
- Right-extending green bars: `IR > 0`.
- Left-extending red bars: `IR < 0`.
- Value column: signed two-decimal number, no `×` suffix (IR is unitless): `+1.45`, `−0.32`, `+0.08`.
- Sort: descending by `IR` (best predictive value on top, worst at bottom).
- "n/a" rows (insufficient data) below the ranked block, italicized and dimmed (same style as today's `ab-unranked`).

### Header

```
WDC · 12 strategies · IR = excess return / strategy std · ticker avg = +5.91%
```

- Drops the `= live Sharpe` badge (irrelevant to IR).
- Keeps the `ticker avg = +X.XX%` badge — provides the baseline reference at a glance.
- Formula gloss in the meta line makes the units self-documenting.

## API changes

`GET /api/portfolio/ticker-alpha/:ticker` payload, per strategy:

```jsonc
{
  "strategy_id":  "S9_dual_momentum",
  "mean_pct":     0.0841,    // existing
  "std_pct":      0.0680,    // NEW — strategy's std of pnl_pct on this ticker
  "n_trades":     23,        // existing
  "ir_raw":       0.514,     // NEW — (mean - baseline) / std
  "ir":           0.453,     // NEW — ir_raw × √min(n,30)/√30
  "alpha":        1.42,      // existing pct-ratio — kept for back-compat
  "dir":          "LONG"     // existing
}
```

Top-level `ticker_mean_pct`, `ticker_n`, and `live_sharpe` all stay. The bar renderer now reads `ir`; the other fields stay for back-compat + tooltip display.

## Frontend changes

`_buildAlphaBarsHtml(group)` rewrites:

- Read `s.ir` per strategy (fall back to `s.alpha` if missing — back-compat for stale cache entries during deploy).
- Center the bar track on the zero line.
- Compute `maxAbs = max(|IR| over strategies with valid IR) || 1`.
- Each bar's `width = |IR| / maxAbs × 50%`.
- Strategies with `IR == null` render as a muted "n/a" row at the bottom, with the existing `.ab-unranked` styling.
- Header drops the `= live Sharpe` badge; keeps `ticker avg = X.XX%`.
- Sort: descending by IR.

## Non-goals

- **Cross-ticker comparison.** IR is per-ticker. We're not aggregating to a portfolio-wide "strategy quality" score from this endpoint.
- **Annualization.** IR stays unitless (no `√252`). The point is comparability *within the chart*, not comparability to external benchmarks.
- **Re-deriving the Sharpe decomposition.** `live_sharpe` still in payload for future use, but the UI no longer surfaces it.

## Verification plan

1. Live check on WDC: at least one strategy with `IR > 1` (high-conviction edge), one with `IR < 0` (destructive), several near zero.
2. Bar widths confirmed symmetric: a `+1.4` IR and a `−1.4` IR should produce mirror-image bars when both present.
3. `tools/page-shot.js` screenshot inspection.
4. Sanity on edge cases:
   - Single-trade strategy → `IR = null`, rendered as italicized "n/a" row.
   - Strategy with all-identical pnl → same fallback.
5. `tools/verify_decomp.js` updated: drop the obsolete `Σ alpha = live_sharpe` invariant (Sharpe decomposition retired from the UI; the invariant is no longer load-bearing).

## Risk + rollback

- API change is additive: new fields added, old fields kept. Older clients keep working.
- Frontend change is contained to `_buildAlphaBarsHtml`. Reverting is a single-file revert.
- No DB schema or live-trading-path impact. Pure read/render change.
