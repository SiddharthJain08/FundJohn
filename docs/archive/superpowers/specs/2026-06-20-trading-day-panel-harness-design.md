# Trading-Day Panel Harness — Design

> **⚠️ SUPERSEDED (2026-06-20)** by
> `2026-06-20-system-wide-trading-day-panel-design.md`. Verification found the
> panel bug is **system-wide** — the live engine (`engine.py:load_prices`) feeds
> the same union panel as the backtest, so a backtest-only flip would diverge
> backtest from live. Phase 1 (the gated backtest helpers) is reused by the
> successor spec; Phases 2-3 here are replaced. Read the successor.

- **Date:** 2026-06-20
- **Status:** SUPERSEDED (Phase 1 implemented + reused; Phases 2-3 replaced)
- **Author:** BotJohn
- **Scope:** `src/backtest/unified_backtest.py` (signal-generation price panel only)
- **Branch:** `feat/intraday-regime-15min-prefetch` (current live branch)

---

## 1. Problem

`unified_backtest.load_prices_panels()` builds the strategy-facing price panel
(`close_wide`) by pivoting `prices.parquet` on a **union** date index:

```python
close_wide = p.pivot(index='date', columns='ticker', values='close')
```

The union index contains **every date any ticker traded**. Because 13
crypto/forex tickers (`BTC-USD`, `ETH-USD`, `…=X`) trade 7 days a week, the
index carries **non-trading days** (weekends + holidays) on which every equity
column is `NaN`.

### Empirical confirmation (2026-06-20)

| Fact | Value |
|---|---|
| Union index rows | **3718** (2016-04-10 → 2026-06-18) |
| Weekend rows in union index | **1059** |
| Tickers contributing weekend bars | **13** (all crypto/forex; `BTC-USD`=1059) |
| `SPY` non-NaN | **2563 / 3718** (0 on weekends) |
| Equity trading calendar (rows with ≥1 equity obs) | **2565** |
| `historical_regimes.parquet` weekend rows | **0** |

### Failure mechanism

The simulate loop is *already* trading-day-gated — `regimes.get(current_date)`
returns `None` on weekend rows (regimes has no weekend rows), so weekend dates
are skipped during iteration. **The bug is the panel's *content*, not the
iteration:** `prices_to_date = close_wide.loc[:current_date]` still *contains*
the interleaved weekend `NaN` rows, so a strategy's internal operations see
them. This breaks:

1. **Wide-frame `pct_change()` / `rolling()`** — every Monday's return is `NaN`
   because the immediately preceding union row is a Sunday crypto bar
   (all-equity-`NaN`). This zeroes any cross-sectional proxy built that way.
2. **Row-count windows** — `prices.tail(N)` / `.iloc[-N:]` span `N` *mixed*
   rows (~5/7 trading), so the effective lookback is ~28% shorter than intended.
3. **Period-start detection** — first-row-of-month logic lands on weekend rows:
   **36 of 123 month-starts** in the union index fall on a weekend, so a
   month-rebalance strategy silently skips those rebalances.

Strategies that operate **per-ticker with `.dropna()`** are unaffected — the
`NaN` rows they already drop simply vanish. **Fills are unaffected** —
`bars_by_ticker` is a per-ticker `groupby` (un-padded), so the t+1 fill price is
always a real bar (verified: BAB 673/700 fills, **0 non-finite**).

---

## 2. The two trigger strategies (reframed)

The two stuck strategies that motivated this work are **not** both
panel-blocked. Empirically:

| Strategy | On **union** panel | Under the **fix** | Verdict |
|---|---|---|---|
| **S_ast_betting_against_beta_factor_in_stocks** | **7266 trades, Sharpe 0.60** (already works) | fix only recovers the ~29% weekend-lost month-starts | **Never panel-blocked.** Earlier 0-trades was a `stops["stop_loss"]` `KeyError`, fixed in `46bff99`. Needs a *committed* re-backtest; clears the 0.5 floor. |
| **S_investor_attention_market_timing** | **0 trades, always** (genuinely blocked) | **0 → 1481 trades, Sharpe −0.30** (Sortino 0.44, hit 27%, DD 42%) | Fix **unblocks evaluation** and reveals **no edge**. Stays candidate / deprecate. |

### Why the fix still matters

The fix's value is **not** "unlock 2 strategies." It removes a **silent-failure
class**: today a cross-sectional strategy with *no edge* and one *broken by the
panel* are indistinguishable — both emit **0 trades**. The fix makes the system
**honestly evaluate** every cross-sectional / single-ticker-proxy port (current
and future), turning "broken" into a real Sharpe. That is the
"similar strategies going down the line" payoff.

---

## 3. Goals / Non-goals

**Goals**
- Equity-class strategies see a price panel on the **equity trading calendar**
  (no crypto/forex weekend rows) so wide-frame math, row windows, and
  period-start detection are correct.
- Preserve crypto behavior (crypto needs the 7-day calendar).
- Default-off until validated; flip to default-on after a parity check
  (operator's validate-then-flip pattern).
- Correct the small set of live cross-sectional strategies currently
  mis-evaluated by the union panel.

**Non-goals**
- No change to `bars_by_ticker` / the fill model.
- No change to `quick_backtest`, `regime_blended_backtest`,
  `intraday_regime_backtest` (separate loaders; assessed as follow-ups).
- No master-data mutation (append-only invariant; we filter in-memory only).
- Not trying to *make IAM profitable* — only to evaluate it honestly.

---

## 4. Design

### 4.1 The single insight

Because iteration is already trading-day-gated, the **entire fix is to filter
`close_wide`'s rows to the equity calendar before the loop**. `oos_dates`,
`prices_to_date`, and `static_universe` all derive from `close_wide`, so they
become clean automatically. `bars_by_ticker` is built and returned separately
and is left untouched.

### 4.2 Components

**`load_prices_panels(calendar='union')`** — new keyword.
- `calendar='union'` (default): current behavior, byte-identical.
- `calendar='equity'`: after the pivot, filter rows to the equity calendar:
  ```python
  EQ = [c for c in close_wide.columns if _is_equity_ticker(c)]
  equity_day = close_wide[EQ].notna().any(axis=1)   # ≥1 equity obs == trading day
  close_wide = close_wide.loc[equity_day.values]
  ```
  `_is_equity_ticker(c)` = `not c.startswith('^') and '-USD' not in c and
  '=F' not in c and '=X' not in c`. Self-contained — **no external
  `alpaca calendar` dependency**. Yields the 2565-row NYSE calendar.
  `bars_by_ticker` is unchanged.

**`run_backtest`** — selects the calendar by instrument class behind the gate:
```python
use_eq_cal = _equity_calendar_enabled() and instrument_class in ('equity','etp','option')
close_wide, bars_by_ticker = load_prices_panels(
    calendar='equity' if use_eq_cal else 'union')
```
`crypto` always receives `union`. The change is inherited by
`run_backtest_with_resolver` and the grid wrapper (both call `run_backtest`).

**Gate `_equity_calendar_enabled()`** — reads `OPENCLAW_BACKTEST_EQUITY_CALENDAR`.
- Phase 1 default: **off** (`'0'`) → prod byte-identical.
- Phase 3 flip: change the default to **on** (one-line edit), so equity/etp/
  option strategies get the aligned panel without requiring the env var.

### 4.3 Data flow

```
prices.parquet
  → pivot (union index)
  → [NEW] equity-calendar row filter   (gated; equity/etp/option only)
  → close_wide
       → oos_dates              (loop iterates trading days only — free speedup)
       → prices_to_date         (per-bar; now NaN-weekend-free)
       → static_universe        (columns; unchanged)
  → generate_signals
fills: bars_by_ticker (per-ticker, un-padded) — UNCHANGED
```

### 4.4 Why this is safe for crypto

`crypto` keeps the union calendar, so `S_btc_momentum` is byte-identical.
(Note: crypto in `unified_backtest` is *already* trading-day-gated by the
equity regime series — a pre-existing quirk outside this fix's scope.)

---

## 5. Blast radius (correct-the-live-book scope)

Of **67** live/monitoring strategies, only **~7** use wide-frame windowing and
can shift under the fix; the other ~60 are per-ticker-`.dropna()` and are
**byte-identical**.

**Cross-sectional (`axis=1`):**
`S_intl_momentum_attention_regime`, `S_3d_pca_characteristic_factors`,
`S_markov_frontier_regimes`, `S_epistemic_rank_gate`,
`S_tr_02_hurst_regime_flip`

**Row-window (`.tail`/`.iloc`):**
`S_price_path_convexity`, `S_nonstationarity_adaptive_selection`

Several of these are likely **mis-evaluated today**; the fix *corrects* their
metrics (a benefit), but the shift may move their regime weights — hence the
validate-then-flip rollout.

---

## 6. Rollout (gated → validate → flip)

**Phase 1 — Build, gate off.**
Implement `load_prices_panels(calendar=…)`, the gate, and the `run_backtest`
selection. Unit tests. Zero prod behavior change (default off).

**Phase 2 — Validate (gate-acceptance).**
Re-backtest the ~7 wide-frame live strategies **both ways** (gate off vs on),
diff Sharpe / trade count / max-DD. Spot-check ~5 per-ticker strategies are
**byte-identical**. Run **chunked, one strategy per subprocess, sequential**
(2-core box, OOM-safe — see `reference_vps_two_core_cpu` /
`project_weekend_refresh_oom_recovery`). Record the diff table in the plan's
results section. Acceptance = shifts are explainable (more trading days in
window) and no strategy silently breaks.

**Phase 3 — Flip default-on.**
Flip the gate default to on, re-backtest the live book (chunked), verify the
dashboard backtest panel + `strategy_weights` moved as expected for the ~7,
and the ~60 are unchanged.

---

## 7. Testing

- **Aligned panel** has 0 weekend rows; equity `prices_to_date.index` ⊆ equity
  calendar; row count 2565 (not 3718).
- **Byte-equivalence:** gate-off `run_backtest` is identical to the current
  implementation (regression guard).
- **Unblock proof:** IAM emits 0 trades with gate off and ~1481 with gate on.
- **Crypto isolation:** a crypto strategy still receives the union panel
  (`load_prices_panels(calendar='union')`), `S_btc_momentum` unaffected.
- **Regression:** `tests/test_unified_backtest_t_plus_1.py` + the unit suite
  stay green.

---

## 8. Out of scope / follow-ups (tracked, not built here)

- **Other backtest loaders** — `quick_backtest._load_prices`,
  `regime_blended_backtest`, `intraday_regime_backtest`: confirm whether they
  share the union-index bug; fix on a separate plan if so.
- **BAB** — commit a real re-backtest (Sharpe 0.60, 7266 trades) and make the
  promotion decision (passes the 0.5 floor). Independent of this harness work.
- **IAM** — Sharpe −0.30 = no edge as implemented. Caveat: its cross-section is
  an alphabetical 600-ticker cap; a universe-resolver-fed cross-section *might*
  differ. Decision: keep candidate / deprecate.
- **Latent wart** — `=X` forex tickers currently leak into `static_universe`
  (tiny; only 2 weekend rows). Noted, not fixed here to preserve gate-off
  byte-equivalence.

---

## 9. Acceptance criteria

1. `OPENCLAW_BACKTEST_EQUITY_CALENDAR` off ⇒ `run_backtest` byte-identical to
   today (regression test passes).
2. With the gate on, an equity strategy's signal panel has no non-trading rows.
3. IAM transitions 0 → ~1481 trades under the gate.
4. Crypto strategies keep the union calendar.
5. Phase-2 diff table produced for the ~7 wide-frame live strategies; ~5
   per-ticker strategies confirmed byte-identical.
6. After Phase-3 flip, the live book is re-backtested and weights/panel
   reconcile with the diff table.
