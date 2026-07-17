# Oxfordstrat Strategy Library → Research Candidates — Design

**Date:** 2026-06-15
**Author:** BotJohn (Claude Code)
**Status:** Design — awaiting operator review before implementation plan

## Goal

Build, backtest, and register a curated set of ~30 publicly-documented systematic
strategies from **oxfordstrat.com** (Stefan Martinek / Oxford Capital Strategies) as
**research candidates**, each with full backtest metrics, on the dashboard candidates
page (`GET /api/strategies`), sorted by Sharpe.

Candidates do **not** trade. Only `strategy_registry.status='approved'` strategies
execute, and `candidate → live` is separately operator-gated. This is a research-page
population task, not a trading change.

## Source material

oxfordstrat.com publishes ~104 free strategy pages — all **daily-bar, Toby-Crabel-style**
systematic rules with fully published entry/exit/stop formulas and numeric parameter
tables. Research (2026-06-15) classified ~100 as reproducible on daily OHLC equity/ETF
bars, 4 partial (need volume / open-interest / a second market), 2 paid with hidden rules
(ALPHA20/DELTA20 — excluded). The author tested on 42 US futures; the rules are generic
single-instrument daily-OHLC formulas that run unchanged on any instrument.

**Author's own ratings (A–D) and our curation:** many pages are author-rated C/D ("ADX
adds no value", "stopped working since 1993", base-case negative Sharpe), and there are
heavy near-duplicate families (23 candlestick variants, 5 Smash Day, 4 Aroon, 12 in the
NR/ORB family). We **curate ~30**: one representative per distinct mechanism, dropping
D-rated losers and near-duplicates.

## Key technical finding — the backtest fill model

`src/backtest/unified_backtest.py:_per_bar_simulate` (verified by reading the code +
CLAUDE.md 2026-05-30 entry + MEMORY.md):

- **Entry is unconditional at `close[t+1]`.** Every emitted signal fills at the next
  bar's close. The signal's `entry_price` is used **only** as `ref` to shape bracket
  geometry, then re-anchored to the actual fill (`_reanchor_bracket`). The engine does
  **not** gate entry on an intraday stop-level being touched.
- **Exits are path-checked per-bar** against OHLC inside `simulate_trade` (stops/targets
  honored intraday from t+2 onward).
- `entry_regime` stays the signal-day (t) regime; `entry_date` is the fill bar (t+1).

**Consequence:** rules whose edge is a CONDITION true at close[t] ("be long while X holds")
map faithfully — entering next bar is exact and slightly conservative. Rules whose edge is
an **intraday stop-entry trigger** ("buy stop at Open+Stretch") would otherwise be taken
unconditionally at close[t+1], measuring a *different* strategy.

### Decision (operator-approved): adapt stop-entry rules as confirmed-breakout daily-bar adaptations

For the ~10 stop-entry breakout rules, the strategy's `generate_signals` computes the
trigger itself from the **signal-day t OHLC**: emit the signal only if the stop level was
pierced during day t (e.g. `High[t] >= Open[t] + Stretch`). The engine then fills at
close[t+1]. Each such strategy's docstring states it is a **daily-bar confirmed-breakout
adaptation** of the Oxford rule — faithful in direction/timing/filtering, **not** a
tick-exact replica of the intraday stop fill. This keeps the full ~30 breadth honestly.
(A future engine enhancement — an intraday-stop fill model — is logged as out-of-scope
follow-up.)

## Architecture

**One-file-per-strategy + a shared helper** (matches the existing convention; each
strategy must be its own `strategy_id` / manifest entry / backtest run to render as a
distinct candidate).

```
src/strategies/implementations/
  oxford_crabel.py        # shared helper module (NOT a strategy)
  oxf_donchian_breakout.py
  oxf_sma_filter.py
  ... (30 thin BaseStrategy subclasses, ids prefixed `oxf_`)
```

### `oxford_crabel.py` — shared helper

Pure, deterministic, no I/O. Provides the common Crabel scaffold so indicator math is
written once:
- Indicators: `atr(bars, n=20)`, Donchian upper/lower channel, `avg_noise`/`stretch`,
  SMA/EMA/HMA/ZLMA/FRAMA/AMA, RSI, MACD, linear-regression slope, Vortex, Aroon,
  Bollinger %b, Keltner band, Heikin-Ashi transform, swing-pivot detection, TD setup/
  countdown counters.
- `OXFORD_ETF_BASKET` — the universe constant (see below).
- Bracket construction delegating to `BaseStrategy.compute_stops_and_targets` (ATR-based,
  regime-scaled) so brackets match the rest of the engine.
- A `confirmed_breakout(bar, level, direction)` helper for the stop-entry adaptation.

### Per-strategy file (thin)

Each subclasses `BaseStrategy`, declares `id` (`oxf_*`), `name`, `description` (citing the
oxfordstrat URL + "daily-bar adaptation" note where applicable), `tier`, `signal_frequency='daily'`,
`min_lookback`, `active_in_regimes`, `default_parameters()` (Oxford's published defaults),
and `generate_signals(prices, regime, universe, aux_data)`. Logic: for each basket ticker,
compute the rule on its OHLC history-to-date, emit a `Signal` with direction + ATR/regime
brackets when the condition (or confirmed breakout) holds; rank; cap at `MAX_SIGNALS`.
Pure, deterministic, fail-soft (return `[]` on missing data, never raise).

`active_in_regimes` defaults by mechanism: **trend/breakout** → `LOW_VOL`, `TRANSITIONING`;
**mean-reversion** → `HIGH_VOL`, `TRANSITIONING`. Backtest is regime-stratified regardless
(tags each trade by its entry-day regime), so per-regime metrics surface even where the
live gate is narrow.

## Universe — liquid ETF proxy basket

Verified present in `data/master/prices.parquet`:
- **Core, full 10y history (2016→2026, ~2559 bars):** SPY, QQQ, IWM, DIA, EFA, EEM, VTI,
  TLT, IEF, SHY, LQD, HYG, AGG, GLD, SLV, USO, UNG, XLE, XLF, XLK, XLV, XLI, XLP, XLU,
  XLY, XLB, GDX (≈27).
- **Commodity/FX extension, ~5.4y (2021→2026, ~1366 bars):** DBC, DBA, DBB, CPER, PALL,
  PPLT, CORN, WEAT, SOYB, MDY, UUP, UDN, FXF.
- **Absent (excluded):** FX euro/yen/pound (FXE/FXY/FXB/FXA/FXC), VIX ETFs (VXX/VIXY), SLX.

Each strategy filters `universe ∩ tickers-present-in-panel ∩ OXFORD_ETF_BASKET` internally,
so it is robust regardless of whether the backtest passes the full panel or a subset.
`strategy_registry.universe` and the manifest are set to the basket for documentation.

## Metrics — persist the full set (add sortino + calmar)

`aggregate_metrics()` already **computes** `sortino` and `calmar`; they are dropped at the
`strategy_backtest_runs` INSERT (columns don't exist). To deliver "proper metrics":

1. **Migration 135** — additive (append-only invariant: columns may be ADDED): add
   `total_sortino NUMERIC`, `total_calmar NUMERIC`, `total_avg_pnl_pct NUMERIC` to
   `strategy_backtest_runs`. Per-regime `sortino`/`calmar` on `strategy_backtest_regimes`
   are added **only if** `aggregate_per_regime` already computes them (verify in the
   slice; `aggregate_metrics` is confirmed to compute the total-level pair). If not, the
   slice either extends `aggregate_per_regime` to compute them or scopes the new metrics
   to the total level only — decided in the slice, not assumed here.
2. **`unified_backtest.run_backtest` INSERT** — include the new columns from the already-
   computed metrics dict. No sim-math change.
3. **`GET /api/strategies` (`server.js`) + `strategy_row.js`** — read and expose
   `backtest_sortino`, `backtest_calmar`, `backtest_avg_pnl_pct` (unified table first,
   null fallback, mirroring the existing sharpe/return/dd contract).
4. **Candidates table UI** — surface Sortino/Calmar columns; **sort candidates by
   `backtest_sharpe` desc** (the table is not server-sorted by Sharpe today — add it).

This benefits **all** strategies, not just the Oxford set (closes the gap that opened this
session). Backfilling sortino/calmar for pre-existing strategies is optional and out of
scope; their next backtest fills the new columns.

## Registration flow (per strategy)

1. Write `oxf_<id>.py`.
2. Manifest entry `state='candidate'`, `metadata.canonical_file`, `metadata.class`,
   `instrument_class='etp'` (via `lifecycle.register(...)` + `save_manifest()`).
3. Backtest: `POSTGRES_URI=... python3 -m backtest.unified_backtest --strategy-file <path>`
   → writes `strategy_backtest_runs/_regimes/_trades` (now incl. sortino/calmar).
4. Ensure a `strategy_registry` row exists (`pending_approval`).
5. Renders on `GET /api/strategies` with full metrics.

NOT added to `_IMPL_MAP` and NOT promoted — candidates only, no live execution.

## Execution plan — vertical slice first

1. **Slice (2 strategies):** build the shared helper + one faithful rule (Donchian or SMA
   filter) + one stop-entry adaptation (NR7). Run migration 135. Backtest both. **Inspect
   `strategy_backtest_trades`** to confirm entries/exits land where the rules say. Confirm
   both render on `/api/strategies` with Sharpe/Sortino/Calmar. Verify universe wiring and
   that the full-panel load doesn't OOM (8GB/no-swap box; backtests run **sequentially**,
   `nice -n 19`).
2. **Fan out** the remaining ~28: parallel code-gen (each thin file is independent), a
   shared contract test, then a **sequential** backtest stage (2-core/OOM constraint),
   then registration.
3. Verify the candidates page lists all ~30 sorted by Sharpe with full metrics.

## Testing

- **Shared-helper unit tests** — indicator math (ATR, Donchian, RSI, MACD, stretch, HA,
  pivots) against hand-computed fixtures.
- **Contract test (all `oxf_*`):** imports, instantiates, `generate_signals` on a fixture
  panel returns valid `Signal`s — no raise, respects `MAX_SIGNALS`, valid directions,
  brackets on the correct side of entry, only basket tickers.
- **Golden tests** — 2-3 representative strategies' signal logic on a tiny fixture
  (a known Donchian breakout, a known NR7 confirmed breakout) assert expected signals.
- Regression: existing backtest + lifecycle suites stay green; migration 135 round-trips.

## The curated 30

**Faithful (state/trend/oscillator — condition at close[t], enter next bar):**
1. `oxf_donchian_breakout` — Donchian channel breakout (donchian-channel-2)
2. `oxf_sma_filter` — Simple MA filter (simple-moving-average)
3. `oxf_adaptive_ma` — Kaufman AMA (adaptive-moving-average-1)
4. `oxf_frama` — Fractal Adaptive MA (fractal-adaptive-moving-average)
5. `oxf_hull_ma` — Hull MA filter (hull-moving-average)
6. `oxf_zero_lag_ma` — Zero-Lag MA filter (zero-lag-moving-average)
7. `oxf_linreg_slope` — Linear-regression slope (linear-regression)
8. `oxf_macd_zero` — MACD zero-line (macd-part-1)
9. `oxf_rsi2_meanrev` — Connors RSI(2) mean-reversion (relative-strength-index-1)
10. `oxf_price_momentum` — Price momentum (price-momentum-model)
11. `oxf_dual_momentum_roc` — Dual momentum & ROC (dual-momentum-rate-of-change)
12. `oxf_vortex` — Vortex indicator (vortex-indicator-1)
13. `oxf_aroon_breakout` — Aroon breakout (aroon-indicator-breakout-1)
14. `oxf_bollinger_momentum` — Bollinger momentum model (bollinger-bands)
15. `oxf_keltner` — Keltner channels 2-phase (keltner-channels-1)
16. `oxf_heikin_ashi` — Heikin-Ashi (heikin-ashi-1)
17. `oxf_livermore` — Livermore swing pivots (livermore-system-1)
18. `oxf_dow_theory` — Dow Theory trend (dow-theory-trend)
19. `oxf_wyckoff_meanrev` — Wyckoff mean reversion (richard-wyckoff-mean-reversion-1)
20. `oxf_td_sequential` — TD Sequential (td-sequential-1)

**Confirmed-breakout adaptations (stop-entry; trigger checked on day-t OHLC):**
21. `oxf_nr7` — NR7 narrow-range breakout (nr7)
22. `oxf_orbp_momentum` — Opening-range breakout + momentum filter (orbp-trend)
23. `oxf_smash_day_b` — Smash Day Type B (smash-day-pattern-b1)
24. `oxf_gap_a` — Gap pattern Type A (gap-pattern)
25. `oxf_greatest_swing_value` — GSV trend (greatest-swing-value-trend)
26. `oxf_welles_wilder_breakout` — Volatility breakout (welles-wilder-1)
27. `oxf_hook` — Crabel hook (pattern-hook)
28. `oxf_bull_oops` — Williams Oops (bull-oops-pattern)
29. `oxf_false_breakout` — False-breakout fade (false-breakout-1)
30. `oxf_ross_hook` — Ross Hook with filter (ross-hook-filter-2)

(Exact list may shift ±1-2 during the slice if a mechanism proves unfit for daily bars;
any drop/substitution is logged.)

## Risks & mitigations

- **Fill-model fidelity (stop-entry rules):** mitigated by the confirmed-breakout
  adaptation + explicit per-strategy documentation; never presented as tick-exact.
- **Author performance ≠ ours:** Oxford's numbers are futures-based, chart-only, and
  unverified. We publish **our** ETF-basket backtest metrics; we do not try to match
  their equity curves.
- **OOM (8GB/no-swap):** sequential backtests, `nice -n 19`; watch RSS during the slice.
- **Research-page noise:** curated set + Sharpe sort keeps winners on top; candidates
  don't trade, so downside is cosmetic.
- **Schema change to a live write path:** migration 135 is strictly additive and
  round-trip-tested; the INSERT change is mechanical; equity path unaffected.

## Out of scope

- Promoting any strategy to live (`_IMPL_MAP` + `candidate→live`).
- An intraday-stop fill model in the engine (logged as follow-up).
- The 4 partial Oxford strategies (volume / OI / intermarket) and the 2 paid systems.
- Backfilling sortino/calmar for pre-existing strategies.
