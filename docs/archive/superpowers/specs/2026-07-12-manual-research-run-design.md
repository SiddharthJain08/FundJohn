# Manual Research Run — 25 new strategies (2026-07-12)

**Status:** EXECUTING (operator-directed session; Fable-5 manual research run)
**Goal:** One-session bulk origination of unique, data-feasible strategies, authored directly
(bypassing PaperHunter/StrategyCoder), registered as `candidate` through the standard
lifecycle + registry paths, marked consumed in `research_candidates` so the weekly research
pipeline never re-creates them, then backtested via the canonical
`backtest.unified_backtest --strategy-file` lane (serial, nice -19).

## Verification method
Each idea was adversarially screened against (a) all 209 manifest strategies + 27 stale
implementation_queue rows + 68 pending research_candidates, and (b) REAL data coverage
checks run against the master parquets. 29 proposed → 25 kept, 4 dropped:
- `social_euphoria_fade`, `social_disagreement_reversal` — sentiment.parquet `social_*`
  columns are ~0% populated (news_* fields are 100% from 2022→).
- `unusual_flow_momentum` — cosmetic variant of live `S_options_flow_confirmed_momentum`.
- `officer_purchase_premium` — reweighted variant of live `S12_insider`.

## Data findings surfaced (operator FYIs)
- **options_aggregates_enriched.parquet is STALE since 2026-04-22** (file mtime Apr 23).
  The daily options-archive keeps growing options_eod, but the enrichment aggregation has
  not run since. Backtest aux `options` slices silently fall back to the 2026-04-22 row for
  every later bar (no staleness cap in `_day_slice`, unlike the sentiment slice). Live
  trading is unaffected (engine.py builds aux from the live chain). Re-running the
  enrichment builder would restore backtest fidelity for all S_HV-family re-backtests.
- sentiment.parquet social_* columns are empty — the social ingestion never populated them.
- historical_regimes.parquet ends 2026-06-05 (bounds every backtest OOS window).
- earnings.parquet actual+estimated coverage: 1,756 events, 378 tickers, 2025-03→2026-03.

## Shared implementation infrastructure
`src/strategies/implementations/_extra_panels.py` (NEW, this session):
- `load_wide(field, tickers, date_floor='2021-01-01')` — chunked, column-pruned,
  float32 wide panels of open/high/low/volume/vwap/transactions from prices.parquet.
  Point-in-time discipline: callers slice `.loc[:asof]` with `asof = prices.index[-1]`.
- `liquid_pool(prices, max_names=500)` — deterministic liquid-ticker pool (close
  completeness ≥90%, price ≥ $5, top-N by 60d median dollar volume) so cross-sectional
  strategies behave identically whether the engine passes SP500 (live resolver) or the
  full ~6k panel (no-resolver backtest).

House conventions every new file follows (template: `S_overnight_intraday_tug_of_war.py`):
BaseStrategy subclass; `INSTRUMENT_CLASS`/`STRATEGY_ID` module constants; empty-input → [];
self-gated cadence (the engine calls every bar); `compute_stops_and_targets` for brackets;
`position_size_pct = base × self.position_scale(regime)`; `[debug] signals=N` stderr line;
`.requirements.json` sidecar; edge must live in ENTRY selection (backtest exits are
stop/target/21d max-hold only).

## The 25 strategies

Sizes: cross-sectional legs use base 0.015 LONG / 0.012 SHORT per name (house norm);
single-instrument timers use 0.5–0.95. All emit at most MAX_SIGNALS per firing.

### Built (template, backtest in flight)
1. **S_overnight_intraday_tug_of_war** (equity) — Lou-Polk-Skouras 2019 JFE. Monthly:
   rank liquid-500 by 60d cumulative log(overnight) − log(intraday) spread; LONG top
   decile (≤12), SHORT bottom (≤12).

### Volume / microstructure family (self-loaded panels, 2021→ coverage)
2. **S_volume_shock_overnight_drift** (equity) — volume z-score vs 60d > 2.5 on an up-day
   (close>prev close) AND day return > +2%: LONG next day (drift continuation). Weekly
   dedupe per name via signal_params cooldown; ≤15/day. Source (marks pending candidate
   DONE): https://www.quantitativo.com/p/volume-shocks-and-overnight-returns
3. **S_retail_trade_size_reversal** (equity) — avg trade size = volume/transactions;
   20d z-score of avg-trade-size CHANGE < −1.5 (shrinking prints = retail herding) AND
   10d return > +8% → SHORT reversal; mirror LONG (rising trade size + −8%). Weekly gate,
   ≤10/leg. Novel use of the `transactions` column (unused anywhere else).
4. **S_vwap_closing_pressure** (equity) — 10d mean of (close−vwap)/vwap; LONG top decile
   (persistent above-VWAP closes = accumulation), SHORT bottom. Names lacking vwap
   coverage excluded by construction. Every 2 weeks, ≤10/leg.
5. **S_amihud_illiquidity_premium** (equity) — Amihud |ret|/(close×volume) 60d mean rank
   within liquid-500; LONG most-illiquid quintile, SHORT least; monthly, ≤12/leg.

### Price-anomaly family (close panel only)
6. **S_max_lottery_demand_reversal** (equity) — MAX5 = mean of 5 largest daily returns in
   trailing 21d; SHORT top decile, LONG bottom decile; monthly (Bali-Cakici-Whitelaw 2011).
7. **S_downside_beta_premium** (equity) — β⁻ (SPY-down days) minus β⁺ (SPY-up days) over
   252d; LONG top quintile of (β⁻−β⁺), SHORT bottom; monthly (Ang-Chen-Xing 2006).
8. **S_52wk_low_capitulation_reversal** (equity) — close within 5% of 252d low AND volume
   z > 2 AND first up-close after ≥3 down days → LONG; ≤10/day, per-name 21d cooldown.
9. **S_same_month_seasonality** (equity) — Heston-Sadka: mean same-calendar-month return
   over prior years (≥4 obs required); LONG top decile at month start (no short leg —
   short seasonality is weak); monthly, ≤12.
10. **S_breadth_divergence_timing** (etp: SPY) — breadth = % of pool above 50d SMA.
    SPY 20d return > +2% while breadth < 45% (negative divergence) → SHORT SPY;
    breadth < 15% then first 5-day breadth improvement → LONG SPY (washout rebound).
    3-day confirmation, 10d cooldown.

### News/sentiment family (2022→ window)
11. **S_no_news_momentum** (equity) — Hong-Lim-Stein 2000: 12-1 momentum long/short but
    ONLY among the lowest-news-coverage tercile (60d sum news_count_24h); monthly, ≤10/leg.
12. **S_sentiment_price_divergence** (equity) — 20d slope z of news_mean_score > +1 while
    20d price return < 0 → LONG (delayed incorporation); mirror SHORT; weekly, ≤8/leg.

### Insider family (insider.parquet 2003→)
13. **S_insider_drawdown_confirmation** (equity) — price ≥30% below 252d high AND trailing
    45d net insider buys ≥ $200k across ≥2 distinct insiders → LONG (informed
    bottom-fishing; conditioning distinguishes it from S12's unconditional clusters).
    ≤8/day, 21d per-name cooldown.
14. **S_insider_seller_strike** (equity) — routine sellers (sells in ≥5 distinct months of
    t−15m..t−3m via insider_history_long) with ZERO sells in trailing 90d → LONG
    (seller silence = private good news; Gao-Ma-Ng). Monthly, ≤10.

### Options/vol family (enriched panel 2024-04→2026-04; equity class, S_HV idiom)
15. **S_pre_earnings_vol_runup** (equity) — aux options: earnings_dte ∈ [5,12] AND
    iv_rank < 40 → BUY_VOL (pre-earnings IV run-up; mirror of S_HV17's post-event fade).
    ≤8/day, one shot per name per earnings cycle.
16. **S_implied_correlation_timing** (etp: SPY) — implied-corr proxy = SPY iv30² ÷
    cap-proxy mean of single-name iv30²; >80th pctile (of trailing 252d) → SHORT SPY,
    <20th → LONG SPY; weekly, 3-day confirmation.

### Vol-term-structure / macro timing (2017→)
17. **S_vix_term_structure_regime_timing** (etp) — aux macro series: VIX9D/VIX and
    VIX/VIX3M both < 0.95 for 3 consecutive days (full-curve contango) → LONG SPY;
    both > 1.02 for 3 days (backwardation) → LONG XLP + XLU (defensive pair), no SPY
    short (asymmetry: backwardation mean-reverts violently). 10d cooldown.

### Calendar/flows (10y, etp)
18. **S_preholiday_effect** (etp: SPY) — LONG SPY at close 2 trading days before each NYSE
    holiday (holiday set computed from deterministic exchange rules in-code), exit via
    brackets/hold (Ariel 1990; Lakonishok-Smidt 1988). ~9 trades/yr × 10y.
19. **S_quarter_end_rebalancing_flows** (etp) — QTD return spread SPY−IEF > +5% entering
    the last 5 sessions of a quarter → SHORT SPY + LONG IEF (institutional rebalancing
    pressure); mirror when < −5%. ~2-4 trades/yr × 10y.

### Crypto (prices.parquet BTC/ETH daily)
20. **S_eth_btc_relative_momentum** (crypto) — weekly: hold whichever of BTC-USD/ETH-USD
    has the higher 90d return IF that return > 0, else flat; 20% vol-target sizing
    (extends S_btc_momentum's single-asset gate to a 2-asset rotation).
21. **S_btc_equity_spillover** (equity) — BTC-USD 10d return > +15% → LONG top-decile
    120d-BTC-correlated SP500 names; < −15% → SHORT them; weekly, ≤8/leg.

### Regime-native (historical_regimes.parquet self-loaded)
22. **S_regime_age_momentum** (equity) — self-computed regime age (consecutive same-state
    days from historical_regimes): age ≤ 10 → 6m momentum LONG top decile (fresh-regime
    trend); LOW_VOL age > 60 → 5d reversal picks instead (aged-regime chop). Weekly, ≤10.
23. **S_crisis_quality_rebound** (equity) — active_in_regimes = ['HIGH_VOL','CRISIS'] ONLY
    (diversifies the fund's thinnest regime coverage): quality names (aux financials
    roe > 15, debt_equity < 1) ≥25% below 252d high whose 5d realized vol is declining →
    LONG; ≤10/day, 10d cooldown. Financials coverage 2025→ (788 tickers) — backtest
    window is the 2025-2026 HIGH_VOL/CRISIS episodes (~63 bars); modest trade count
    accepted for regime-coverage value.

### Factor structure
24. **S_factor_momentum_rotation** (equity) — internal price factors (12-1 momentum,
    low-vol, 5d short-term reversal) as daily top-minus-bottom-decile spread series;
    monthly, allocate LONG the current top-decile names of each factor whose trailing
    63d factor return > 0 (factor momentum, Gupta-Kelly 2021); ≤15.
25. **S_earnings_sue_pead** (equity) — true SUE = (eps_actual−eps_estimated)/|eps_estimated|
    from earnings.parquet; within 3 days after report date: LONG top-SUE quintile
    (SUE > +10%), SHORT bottom (< −10%); ≤10/day. Window 2025-03→ (shallow — flagged;
    grows every quarter; the only PEAD in the book using real analyst estimates).

## Integration & completion marking (why the system won't re-create these)
1. Files land in `src/strategies/implementations/` — `_codeFromQueue` skips StrategyCoder
   when a canonical file exists (hand-coded protection).
2. Manifest registration (state=candidate) via `LifecycleStateMachine.register` +
   `save_manifest` (cross-process lock + merge) → ResearchJohn's classify context carries
   manifest ids → dedupe at classification.
3. `strategy_registry` INSERT (status='pending_approval', ON CONFLICT DO NOTHING) —
   mirrors `_registerStrategy`; required because the skip-coding path never inserts.
4. `research_candidates` rows: source_url = canonical reference URL (or
   `manual://fable-research/2026-07-12/<sid>` where no external source), origin per lane,
   `status='done'`, `data_tier='A'`, full `hunter_result_json` (strategy_id, hypothesis,
   signal_logic, data_requirements, universe, instrument_class) → corpus-promotion
   `NOT EXISTS(source_url)` dedupe + finisher terminal-status dedupe. The already-pending
   quantitativo volume-shocks row is UPDATEd to done (consumed by #2).
5. `python3 src/strategies/generate_signatures.py` → similarity fingerprints current.
6. Backtests: serial detached runner, `nice -19`, one at a time:
   `PYTHONPATH=src python3 -m backtest.unified_backtest --strategy-file <abs path>` then
   `python3 -m backtest.eligibility_assigner --strategy-id <sid>`; registry mirror metrics
   updated like the orchestrator does. Candidates surface on the Research dashboard for
   the operator's promote/reject click — nothing auto-promotes (house invariant).
