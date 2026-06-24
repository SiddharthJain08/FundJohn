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

---

## 10. Phase 3 results — full-book off→on diff (2026-06-23)

Satisfies acceptance criterion #6. Each strategy was backtested twice through
the *same* `run_backtest` harness — `g0` = `OPENCLAW_EQUITY_TRADING_CALENDAR`
OFF (union panel, 3718 dates), `g1` = ON (equity panel, 2565 dates). Sweep ran
detached, nice-19, sequential, 1.5 GB MemAvailable floor.

**Verdict: PASS. 65/67 strategies swept, ZERO pathologies.** No strategy
crashed, produced NaN/None Sharpe, or went `trades→0` where it had traded on the
union panel. Every g1 change is one of three benign outcomes:

- **Unlock (the headline fix).** `S_price_path_convexity` — the first of the two
  target strategies — goes `0 → 123,212` trades. The union panel had it
  silently dead; the equity panel lets it trade **and** renders an honest verdict
  (Sharpe −1.35 = correctly unprofitable). This is logical soundness, not a
  return improvement.
- **De-inflation (intended).** Inflated union Sharpes collapse to their true
  values once weekend-zero bars are removed: `epistemic_rank_gate` 4.99→2.70,
  `extreme_intraday_reversal_nasdaq` 4.68→2.30, `tr_06_eod_reversal` 4.40→2.17,
  `barbell_trend_horizon` 2.65→0.57, `price_earnings_momentum_drift` 4.95→3.01.
  A lower g1 Sharpe is the correction working, **not** a pathology. Some move the
  other way (`cross_sectional_price_momentum` 1.75→2.96, `ptree_panel_tangency`
  1.76→2.80) — removing weekend ffill/vol distortion is direction-agnostic.
- **Byte-identical.** Already-equity-only strategies (`S_HV*`, `S12_insider`,
  `news_sentiment_long_short`, `S21_iv_hv_spread`) show ΔSharpe 0.0000.

**Two inert (dead under BOTH gates → genuinely dead, not a calendar artifact):**
`S_vp_macd_index_sensitivity` (the second target strategy — confirmed dead, not
poisoned) and `S15_insider_opportunistic_short`.

**Two pending (slow tail, not yet swept):** `S_tr_03_bocpd_change_point` and
`S_pairs_trading_jump_diffusion_intraday`. BOCPD is ~O(T²) over the 3718-date
union panel and ran >22 h on a single g0 backtest before this table was cut.
This slowness is a **backtest-sim artifact only** — the live path
(`engine.py:load_prices` → per-day signal generation) does not run the historical
sim, so the calendar flip does not make these strategies slow in production.
They will be validated on a bounded window separately; they do not gate Phase 4.

Merge tooling: `/tmp/panel_merge.py` (overrides the original contaminated sweep
with the corrected rerun per (sid,gate); raw table `/tmp/panel_phase3_table.md`).

> **Methodology note — harness contamination caught & corrected.** The first
> overnight sweep set the *dead pre-rename* gate name
> `OPENCLAW_BACKTEST_EQUITY_CALENDAR`; current code reads only
> `OPENCLAW_EQUITY_TRADING_CALENDAR`, so the gate silently no-op'd and 18
> strategies came back falsely byte-identical (measuring union-vs-union). The
> **shipped production code was never affected** — only the throwaway runner. The
> corrected 106-run sweep below confirms a probe triple (off=2850/0.50,
> dead-name=no-effect, live-name=equity-active).

| # | Strategy | g0 trades | g0 Sharpe | g0 maxDD% | g1 trades | g1 Sharpe | g1 maxDD% | ΔSharpe | Δtrades | flag |
|---|----------|-----------|-----------|-----------|-----------|-----------|-----------|---------|---------|------|
| 1 | S_intl_momentum_attention_regime | 70734 | 2.5343 | 8.94 | 21147 | 1.4400 | 18.97 | -1.0943 | -49587 |  |
| 2 | S_3d_pca_characteristic_factors | 2850 | 0.5005 | 10.27 | 5500 | 0.3358 | 16.57 | -0.1647 | 2650 |  |
| 3 | S_markov_frontier_regimes | 9714 | 1.1524 | 18.29 | 9204 | 1.4134 | 18.29 | 0.2610 | -510 |  |
| 4 | S_epistemic_rank_gate | 116473 | 4.9889 | 4.50 | 112166 | 2.6989 | 14.23 | -2.2900 | -4307 |  |
| 5 | S_tr_02_hurst_regime_flip | 116272 | 1.5013 | 5.37 | 111544 | 0.3317 | 6.83 | -1.1696 | -4728 |  |
| 6 | S_price_path_convexity | 0 | — | 0.00 | 123212 | -1.3477 | 38.80 | — | 123212 | UNLOCK |
| 7 | S_nonstationarity_adaptive_selection | 95265 | -1.3492 | 79.92 | 122977 | -0.6120 | 72.20 | 0.7372 | 27712 |  |
| 8 | S_reversal_momentum_transition_earnings | 106150 | 1.8038 | 7.24 | 102200 | 2.3079 | 6.23 | 0.5041 | -3950 |  |
| 9 | S_ptree_panel_tangency | 113787 | 1.7645 | 15.22 | 113776 | 2.8025 | 14.64 | 1.0380 | -11 |  |
| 10 | momentum_12_1 | 22799 | 1.2299 | 50.56 | 22749 | 1.2412 | 50.56 | 0.0113 | -50 |  |
| 11 | S_vp_macd_index_sensitivity | 0 | — | 0.00 | 0 | — | 0.00 | — | 0 | ⚪ inert |
| 12 | S_price_earnings_momentum_drift | 109737 | 4.9531 | 5.10 | 101896 | 3.0097 | 9.12 | -1.9434 | -7841 |  |
| 13 | S9_dual_momentum | 9195 | 0.4127 | 66.41 | 9170 | 0.4276 | 66.41 | 0.0149 | -25 |  |
| 14 | S12_insider | 276 | -1.0178 | 39.27 | 276 | -1.0178 | 39.27 | 0.0000 | 0 |  |
| 15 | S_custom_jt_momentum_12mo | 11410 | 0.6850 | 56.96 | 11385 | 0.6969 | 56.96 | 0.0119 | -25 |  |
| 16 | S23_regime_momentum | 4995 | -0.3903 | 92.95 | 4959 | -0.2154 | 83.06 | 0.1749 | -36 |  |
| 17 | S24_52wk_high_proximity | 4612 | -0.2796 | 25.71 | 4470 | -0.8054 | 29.15 | -0.5258 | -142 |  |
| 18 | S25_dual_momentum_v2 | 16131 | 0.9111 | 48.52 | 15939 | 0.9365 | 48.52 | 0.0254 | -192 |  |
| 19 | S_HV8_gamma_theta_carry | 21181 | 0.8488 | 11.86 | 21181 | 0.8488 | 11.86 | 0.0000 | 0 |  |
| 20 | S_HV16_gex_regime | 2126 | -3.7761 | 44.62 | 2126 | -3.7761 | 44.62 | 0.0000 | 0 |  |
| 21 | S_HV19_iv_surface_tilt | 2128 | 2.5571 | 17.19 | 2128 | 2.5571 | 17.19 | 0.0000 | 0 |  |
| 22 | S_HV20_iv_dispersion_reversion | 2215 | -0.6743 | 13.60 | 2215 | -0.6743 | 13.60 | 0.0000 | 0 |  |
| 23 | S_tr_06_eod_reversal | 125780 | 4.4009 | 2.62 | 125705 | 2.1729 | 4.51 | -2.2280 | -75 |  |
| 24 | S_barbell_trend_horizon | 118531 | 2.6538 | 18.20 | 114658 | 0.5665 | 38.94 | -2.0873 | -3873 |  |
| 25 | S_sparse_basis_pursuit_sdf | 21886 | 1.0804 | 37.79 | 20326 | 1.0152 | 37.79 | -0.0652 | -1560 |  |
| 26 | S21_iv_hv_spread | 5172 | 0.0088 | 7.15 | 5172 | 0.0088 | 7.15 | 0.0000 | 0 |  |
| 27 | S22_quality_momentum | 18227 | 1.4117 | 39.24 | 18179 | 1.5647 | 37.21 | 0.1530 | -48 |  |
| 28 | S25_dual_momentum | 16131 | 0.9111 | 48.52 | 15939 | 0.9365 | 48.52 | 0.0254 | -192 |  |
| 29 | low_volatility_us | 118073 | -7.8289 | 3.49 | 114176 | -6.6508 | 3.26 | 1.1781 | -3897 |  |
| 30 | S_cross_sectional_price_momentum | 102199 | 1.7495 | 19.20 | 102150 | 2.9610 | 11.89 | 1.2115 | -49 |  |
| 31 | S_ivol_mispricing_asymmetry | 84152 | 0.7627 | 13.43 | 101780 | 0.8439 | 13.36 | 0.0812 | 17628 |  |
| 32 | S_extreme_intraday_reversal_nasdaq | 68498 | 4.6770 | 10.66 | 65296 | 2.2963 | 30.48 | -2.3807 | -3202 |  |
| 33 | S_ma_tsmom_crossover | 115611 | 0.7370 | 16.98 | 115459 | 0.7617 | 16.97 | 0.0247 | -152 |  |
| 34 | S_long_term_price_reversal | 78375 | -0.9964 | 50.02 | 88195 | -1.1119 | 38.87 | -0.1155 | 9820 |  |
| 35 | S_macro_risk_momentum_ip_beta | 22339 | 1.4518 | 24.24 | 21979 | 1.4382 | 21.84 | -0.0136 | -360 |  |
| 36 | S_pca_etf_stat_arb_reversion | 96450 | 0.1416 | 19.41 | 48100 | 0.4192 | 16.59 | 0.2776 | -48350 |  |
| 37 | S_btc_gold_dual_momentum_rotation | 1596 | 0.5928 | 39.39 | 1567 | 0.6251 | 39.39 | 0.0323 | -29 |  |
| 38 | S_prism_vq_cross_section_factor | 23784 | 2.7205 | 9.62 | 22904 | 1.9989 | 8.66 | -0.7216 | -880 |  |
| 39 | S_visibility_graph_rsi | 123582 | 0.4583 | 7.16 | 123533 | 0.4647 | 7.16 | 0.0064 | -49 |  |
| 40 | S_price_filter_rule_trend | 83119 | -0.0381 | 54.29 | 76710 | 0.5344 | 29.05 | 0.5725 | -6409 |  |
| 41 | S_labor_day_week_momentum_reversal | 9 | 4.7845 | 2.69 | 8 | 5.4759 | 2.09 | 0.6914 | -1 |  |
| 42 | S_fomc_presell_spy_long | 5 | 4.6535 | 2.77 | 5 | 6.5540 | 1.91 | 1.9005 | 0 |  |
| 43 | S_bppp_bayesian_parametric_weights | 62800 | 0.8758 | 4.99 | 62619 | 0.3316 | 9.69 | -0.5442 | -181 |  |
| 44 | S_growth_inflation_sector_timing | 2202 | 0.3379 | 41.88 | 2046 | 0.2858 | 41.88 | -0.0521 | -156 |  |
| 45 | S_fama_french_anomaly_dissection | 82998 | 2.9689 | 12.06 | 63496 | 3.0162 | 12.06 | 0.0473 | -19502 |  |
| 46 | S_value_momentum_everywhere | 82826 | 3.2395 | 10.18 | 63312 | 3.3041 | 9.83 | 0.0646 | -19514 |  |
| 47 | S_idiosyncratic_vol_puzzle | 88080 | 1.4940 | 17.07 | 81840 | 1.7009 | 14.50 | 0.2069 | -6240 |  |
| 48 | S_commodity_etp_momentum | 2465 | 0.9116 | 38.92 | 2460 | 0.9302 | 38.92 | 0.0186 | -5 |  |
| 49 | S_btc_momentum | 1491 | 0.9801 | 61.07 | 1491 | 0.9801 | 61.07 | 0.0000 | 0 | crypto=union |
| 50 | S15_insider_opportunistic_short | 0 | — | 0.00 | 0 | — | 0.00 | — | 0 | ⚪ inert |
| 51 | S_options_flow_confirmed_momentum | 208 | -2.6334 | 63.95 | 208 | -2.6334 | 63.95 | 0.0000 | 0 |  |
| 52 | S_news_sentiment_long_short | 40549 | 1.2368 | 7.08 | 40549 | 1.2368 | 7.08 | 0.0000 | 0 |  |
| 53 | S_ast_asset_class_trend_following | 194 | 2.0283 | 12.40 | 308 | 2.9306 | 10.73 | 0.9023 | 114 |  |
| 54 | oxf_adaptive_ma | 61856 | -0.4902 | 12.88 | 61731 | -0.5042 | 12.88 | -0.0140 | -125 |  |
| 55 | oxf_false_breakout | 4039 | 0.4617 | 29.18 | 4030 | 0.4183 | 29.18 | -0.0434 | -9 |  |
| 56 | oxf_frama | 36523 | -0.1273 | 14.49 | 36446 | -0.1545 | 14.49 | -0.0272 | -77 |  |
| 57 | oxf_heikin_ashi | 63125 | -0.0034 | 5.80 | 63000 | 0.0003 | 5.80 | 0.0037 | -125 |  |
| 58 | oxf_keltner | 42281 | -0.3582 | 12.58 | 42215 | -0.3374 | 12.58 | 0.0208 | -66 |  |
| 59 | oxf_linreg_slope | 53398 | -0.1331 | 22.78 | 53336 | -0.1573 | 22.78 | -0.0242 | -62 |  |
| 60 | oxf_macd_zero | 62875 | -0.2075 | 13.62 | 62750 | -0.1931 | 13.62 | 0.0144 | -125 |  |
| 61 | oxf_rsi2_meanrev | 1857 | 1.1047 | 20.05 | 1856 | 1.0768 | 20.05 | -0.0279 | -1 |  |
| 62 | oxf_sma_filter | 54385 | 0.5656 | 17.41 | 54269 | 0.5922 | 17.41 | 0.0266 | -116 |  |
| 63 | oxf_smash_day_b | 7330 | 0.7339 | 14.28 | 7317 | 0.7558 | 14.28 | 0.0219 | -13 |  |
| 64 | oxf_vortex | 2757 | 0.9406 | 11.76 | 2752 | 0.8972 | 11.76 | -0.0434 | -5 |  |
| 65 | oxf_zero_lag_ma | 48279 | 0.0178 | 16.01 | 48175 | 0.0158 | 16.01 | -0.0020 | -104 |  |
| — | S_tr_03_bocpd_change_point | PENDING | — | — | PENDING | — | — | — | — | slow (O(T²)); live unaffected |
| — | S_pairs_trading_jump_diffusion_intraday | PENDING | — | — | PENDING | — | — | — | — | slow; live unaffected |

**Phase 4 (operator-gated):** with 0 pathologies across 65/67, the equity-calendar
flip is clear from a correctness standpoint. The 2 pending tail strategies are a
backtest-speed footnote, not a flip blocker. Flip steps unchanged from §6.

### Phase 4 — EXECUTED 2026-06-24 (LIVE)

Operator authorized the flip. `OPENCLAW_EQUITY_TRADING_CALENDAR=1` added to `.env`
and johnbot (`--user`) restarted; verified the gate is in the live process env,
`equity_calendar_enabled()=True`, `calendar_for('equity')→equity`,
`calendar_for('crypto')→union`, NRestarts=0, clean startup.

**Unlock-handling (the non-obvious risk fail-open does NOT cover).** Fail-open
(`engine.py` per-strategy `try/except`) protects against *crashes*, not *unlocks*:
a currently-inert approved strategy can run fine on the equity panel and start
trading a negative-Sharpe book (exactly `price_path_convexity`). Discriminator =
live-signal activity (`execution_signals`), since `registry.backtest_trade_count`
is mostly NULL. Of 80 approved, 67 were swept clean; the 13 unswept-equity split
into currently-active (flip only de-inflates → safe) and truly-inert (0 live
signals → unlock unknowns). The truly-inert set was spot-checked on the equity
panel; operator rule = **deprecate any inert strategy the flip unlocks into a
negative-Sharpe book**. Deprecated (registry `status='deprecated'`, reversible):

| Strategy | equity-panel verdict |
|----------|----------------------|
| `S_price_path_convexity` | 123,212 trades, Sharpe −1.35 |
| `S_HV11_cross_stock_dispersion` | 1,095 trades, −0.95 |
| `S_HV15_iv_term_structure` | 2,321 trades, −1.87 (1.5y window) |
| `S_quantum_rebalance_qaoa` | 459 trades, −0.65 (2mo; un-backtestable longer) |

Kept (confirmed stay-dead, 0 trades on equity panel → harmless inert):
`S10_quality_value`, `S_HV17_earnings_straddle_fade`, `S_bankruptcy_risk_anomaly`,
`S_skewness_dispersion_macro`, `S_local_global_balance`. `bocpd`/`pairs` are
live-active (743/18 signals) so the flip only de-inflates them — no slow backtest
needed to flip. Approved book 80 → 76.

**Remaining:** (1) monitor first post-flip compute (errored count + unlock-flood +
deprecated-emit-zero); (2) reweight — committed equity-panel re-backtest CHUNKED
per-strategy gate-ON then `strategy_weights --rebuild` (deferred; stale weights ==
today's weights, so non-acute). **Never `refresh_backtests.sh --all-live`** — its
monolithic loop global-OOMs the 8 GB box.
