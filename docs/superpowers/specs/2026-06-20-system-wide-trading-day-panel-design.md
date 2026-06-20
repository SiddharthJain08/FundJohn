# System-Wide Trading-Day Panel — Design

- **Date:** 2026-06-20
- **Status:** Approved (design); implementation pending
- **Author:** BotJohn
- **Supersedes:** `docs/superpowers/specs/2026-06-20-trading-day-panel-harness-design.md` (backtest-only — proven insufficient; see §1)
- **Scope:** `src/execution/engine.py` (live signal panel) + `src/backtest/unified_backtest.py` (backtest panel) + new `src/lib/price_panel.py`
- **Branch:** `feat/intraday-regime-15min-prefetch`

---

## 1. Problem

The strategy-facing price panel is built by pivoting `prices.parquet` on a
**union** date index in **both** the live and backtest paths. Because 13
crypto/forex tickers trade 7 days a week, that index carries **1059
weekend/holiday rows** (2016-04→2026-06) on which every equity column is `NaN`.

- **Backtest:** `unified_backtest.load_prices_panels` → `p.pivot(...)`
- **Live:** `engine.py:load_prices` → `df.pivot(index='date', columns='ticker', values='close')` (line 324). It filters **columns** to the universe (line 327-329) but **never drops the weekend rows** — they remain in the index, all-NaN for equities.

So `run_strategies` → `generate_signals` receives the same weekend-contaminated
panel **live as in backtest.**

### Failure mechanism (verified on `S_epistemic_rank_gate`, 2026-06-20)

Cross-sectional / single-ticker-proxy strategies compute over the panel in ways
that the weekend rows distort:

- `price_data = prices[tickers].ffill()` forward-fills weekend gaps with Friday's
  price → weekend rows become **zero-return** bars.
- `start_px = price_data.iloc[-(252+21)]` counts **rows**, not trading days. With
  ~2/7 of rows being weekends, a "252-trading-day" momentum window actually spans
  **~195 trading days** (~9 months, not 12).
- `vol = returns.iloc[-21:].std() * sqrt(252)` includes forward-filled weekend
  zero-returns → **understated vol → oversized positions**, and the √252
  annualization is applied to a calendar-contaminated window.

Result: `epistemic_rank_gate` backtests **Sharpe +4.99** on the union panel vs
**+2.70** on the equity calendar. The +4.99 is the strategy running on
**distorted windows**; +2.70 is it running **as designed**. Several cross-sectional
strategies show this (`intl_momentum` +2.53→+1.44 with 70k→21k trades; `3d_pca`
+0.50→+0.34; `markov_frontier` +1.15→+1.41 improves; per-ticker rankers
`ptree_panel_tangency` 1.76→2.80, `reversal_momentum` 1.80→2.31).

### Why backtest-only was insufficient

Since **live uses the same union panel**, the union-panel backtest currently
*matches* live mechanics. Flipping only the backtest to the equity calendar would
make backtest metrics (e.g. +2.70) **diverge from live behavior** (+4.99
distorted) — and because regime **weights are derived from backtest Sharpes**, the
live book would be weighted on numbers that no longer describe how it trades. The
fix must therefore be **system-wide and flip live + backtest together.**

---

## 2. Guiding principle (operator-set)

**Logical soundness over returns.** The equity trading calendar is the
*correct* panel: a strategy declaring a 252-trading-day lookback should get 252
trading days, and vol should be measured over real sessions. A strategy that
scores *higher* on the distorted union panel is benefiting from a bug "for no
apparent reason" — that is not a real edge, and de-inflating it is the right
outcome even though its backtested Sharpe drops. We are correcting the system's
logic; the backtest diff measures the impact, it does not gate the correctness
decision. (Per operator, 2026-06-20: no dual-compute live shadow — rely on the
backtest diff, flip live directly after build.)

---

## 3. Goals / Non-goals

**Goals**
- Equity/etp/option strategies see a panel on the **equity trading calendar** in
  **both** live and backtest, controlled by **one** gate so the two can never
  diverge.
- Crypto strategies keep the union (7-day) calendar.
- Single shared definition of "equity trading day" — no drift between paths.
- Default OFF → byte-identical to today everywhere until the deliberate flip.

**Non-goals**
- No master-data mutation (in-memory filtering only — append-only invariant).
- No change to `bars_by_ticker` / the backtest fill model (fills already correct).
- No dual-compute live shadow sidecar (operator decision).
- Not making any specific strategy profitable — only making the panel correct.

---

## 4. Design

### 4.1 Shared module — `src/lib/price_panel.py`

Single source of truth, imported by both paths:

```python
import os
import pandas as pd

GATE = 'OPENCLAW_EQUITY_TRADING_CALENDAR'

def is_equity_ticker(ticker: str) -> bool:
    """Equity/ETF vs index (^…) / crypto (…-USD) / future (…=F) / forex (…=X)."""
    t = str(ticker)
    return (not t.startswith('^')) and ('-USD' not in t) and ('=F' not in t) and ('=X' not in t)

def apply_equity_calendar(close_wide: pd.DataFrame) -> pd.DataFrame:
    """Keep only rows with ≥1 non-NaN equity observation (the equity trading
    calendar). Drops crypto/forex-only weekend/holiday rows. Rows only."""
    eq = [c for c in close_wide.columns if is_equity_ticker(c)]
    if not eq:
        return close_wide
    return close_wide.loc[close_wide[eq].notna().any(axis=1).values]

def equity_calendar_enabled() -> bool:
    return os.environ.get(GATE, '0') == '1'

def calendar_for(instrument_class: str) -> str:
    if equity_calendar_enabled() and instrument_class in ('equity', 'etp', 'option'):
        return 'equity'
    return 'union'
```

### 4.2 Backtest wiring (repoint Phase 1 to the shared module)

`unified_backtest.py` already has `_is_equity_ticker`/`_apply_equity_calendar`/
`_equity_calendar_enabled`/`_calendar_for` + `load_prices_panels(calendar=…)` +
the `run_backtest` dispatch (Phase 1, commits `3fdc755..edcfe08`). Change those
four module-level helpers to thin re-exports of the shared module (so the gate
name unifies to `OPENCLAW_EQUITY_TRADING_CALENDAR`); `load_prices_panels` and
`run_backtest` keep their current shape. Net behavior change: the gate is renamed.

### 4.3 Live wiring — `engine.py:run_strategies`

`load_prices` is unchanged (returns the union panel — it serves mixed instrument
classes). The per-strategy calendar is applied where the panel is already sliced
per strategy (line ~885-892), using the `instrument_class_for(strat.id)` the
function already computes (line 869):

```python
from lib.price_panel import apply_equity_calendar, calendar_for
...
# after strat_prices is determined (either the universe slice or the shared panel):
if calendar_for(instrument_class_for(strat.id)) == 'equity':
    strat_prices = apply_equity_calendar(strat_prices)
signals = strat.generate_signals(strat_prices, strat_regime, strat_universe, strat_aux)
```

Equity/etp/option → equity calendar; crypto → union (byte-identical). Gate OFF →
`calendar_for` returns `'union'` for all → byte-identical to today.

### 4.4 One gate, both paths

`OPENCLAW_EQUITY_TRADING_CALENDAR` (default OFF) controls **both** via the shared
`calendar_for`. Flipping it moves live signal computation **and** backtest metrics
to the equity calendar in lockstep → they stay matched. The gate doubles as the
instant kill-switch.

---

## 5. Validation

Per the operator's principle, validation is **impact-visibility, not a
correctness gate**:

1. **Full-book backtest diff (running):** all 67 live/monitoring strategies,
   union vs equity, ephemeral. Since live==backtest mechanics, this **is** the
   faithful estimate of the live behavior change. Use it to (a) confirm no
   strategy goes pathological (crash / NaN / 0-trades-where-it-shouldn't) and
   (b) see the magnitude per strategy. It does **not** veto the flip for a
   merely-lower Sharpe (that's the de-inflation we want).
2. **Re-backtest the book on the equity panel at flip time** so DB metrics +
   `strategy_weights` reflect the correct panel (chunked, sequential, OOM-safe).
3. **No dual-compute live shadow** (operator decision).

---

## 6. Rollout

- **P1 — done (reused):** backtest equity-calendar helpers (`3fdc755..edcfe08`).
- **P2 — shared module + live wiring, gate OFF.** Create `src/lib/price_panel.py`;
  repoint backtest helpers to it; wire `run_strategies`. Unify gate name. TDD.
  Prod byte-identical (gate OFF). Deploy is safe.
- **P3 — review the full-book backtest diff** (the running sweep) for pathologies
  + magnitude.
- **P4 — flip `OPENCLAW_EQUITY_TRADING_CALENDAR=1`** (live + backtest together),
  re-backtest the book on the equity panel, refresh weights, restart johnbot,
  monitor the first live EOD compute. Gate stays as kill-switch.

---

## 7. Risks & mitigations

- **First post-flip cycle repositions the cross-sectional book** (e.g.
  `intl_momentum` ~70% fewer signals). The sizer is delta-based
  (`target − current`), so positions **net**, not blow out. Monitor the first
  16:15 ET compute; the gate is an instant revert.
- **Weights shift** as de-inflated Sharpes lower some weights and raise others —
  this is the intended correction; re-backtest + reweight at flip so the book
  reflects the correct panel.
- **Crypto untouched** — `calendar_for('crypto') == 'union'`; `S_btc_momentum`
  byte-identical (regression test).
- **Divergence impossible by construction** — one gate, one shared definition.

---

## 8. Acceptance criteria

1. `OPENCLAW_EQUITY_TRADING_CALENDAR` OFF ⇒ both `run_backtest` and
   `run_strategies` byte-identical to today (regression tests, both paths).
2. With the gate ON, an equity strategy's panel (live and backtest) has zero
   non-trading rows; crypto keeps the union calendar in both.
3. `engine.py` and `unified_backtest.py` import the *same* `apply_equity_calendar`
   / `calendar_for` from `src/lib/price_panel.py` (no duplicated definition).
4. `S_btc_momentum` (crypto) backtest is byte-identical gate-on vs gate-off.
5. `S_investor_attention_market_timing` transitions 0 → ~1481 trades under the
   gate (the unblock, now in both live and backtest).
6. Full-book diff produced; no strategy goes pathological (crash/NaN); magnitude
   table recorded.
7. Post-flip: book re-backtested on the equity panel, weights refreshed, first
   live compute monitored.

---

## 9. Follow-ups (not in this spec)

- **BAB** — commit a real re-backtest (0.60, 7266 trades) + promotion decision.
- **IAM** — Sharpe −0.30 on the correct panel = no edge; keep candidate / deprecate.
- Other backtest loaders (`quick_backtest`, `regime_blended_backtest`,
  `intraday_regime_backtest`) — confirm whether they pivot the union index too;
  fold into the shared module if so.
- `=X` forex tickers in `static_universe` (tiny; pre-existing).
