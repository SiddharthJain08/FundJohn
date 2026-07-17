# Cross-Sector Corroboration Equity Strategies — Design

**Date:** 2026-05-29
**Author:** BotJohn (brainstormed with operator)
**Status:** Design — approved in brainstorming, pending spec review

## 1. Goal

Add **three new `instrument_class='equity'` strategies** that take a base directional
equity signal and *corroborate* it with data from an adjacent domain, then **backtest and
verify each is functional and profitable before any promotion to the live stack**:

1. **Options-flow corroboration** — confirm bullish equity signals with heavy call demand
   (low Put-Call ratio / call-skewed IV) and bearish signals with heavy put demand.
2. **Sector & ETF flow** — confirm a stock signal only when its sector SPDR and the broad
   market (SPY/QQQ) move in alignment; plus a basket mode that goes **long the constituents
   of the strongest-trending sector(s) and short the constituents of the weakest-trending
   sector(s)**.
3. **News-sentiment-primary long/short** — use the news/sentiment ingester directly as the
   *primary* signal (long strongly-positive, short strongly-negative sentiment names).

## 2. Decisions locked in brainstorming

- **Hybrid build** for ① and ②: each is a complete **standalone** strategy (independently
  backtestable / promotable) whose corroboration logic is factored into a **reusable
  pure-function module** that other strategies (or the TradeJohn confirmer) can import later.
- **Strategy ③ path:** backfill historical news first → FinBERT-score → build a real
  point-in-time sentiment series → then backtest. (Chosen over forward-test-only and
  proxy-backtest because the operator wants profitability proven on real data first.)
- **Base signal** for ① and ②: simple **cross-sectional momentum/trend**, **long & short**.
- **Sector map** for ②: **static curated GICS** ticker→sector→SPDR map (no API dependency).
- **Promotion is operator-gated.** Nothing auto-promotes; passing strategies stay `candidate`
  and the operator gets the numbers for a promote decision.
- Weight: **execution-leaning** (rigor over token-thrift), parallelize where safe.

## 3. Non-goals / scope boundaries

- Not trading options *contracts* — ① reads options *flow* but trades the underlying equity.
  (`instrument_class='equity'`, `option_spec=None`.)
- Not touching the live execution path / sizer / handoff for ① and ②. The reusable
  confirmation modules are *available* for live reuse but wiring them into the confirmer is a
  separate future decision.
- Not reconstructing historical Reddit/StockTwits social sentiment — those are real-time-only.
  ③ is backtested **news-only**; the live signal will additionally include social (documented gap).
- No master-data deletion. The news backfill is **append-only** (honors the NEVER-DELETE invariant).

## 4. Data feasibility (verified 2026-05-29)

| Domain | Source (verified) | Backtest depth | Verdict |
|---|---|---|---|
| **PCR** | `data/master/options_aggregates_enriched.parquet` → `aux_data['options'][t]['pc_ratio']` (= put_call_vol_ratio, volume-derived) | 2024-04-22 → 2026-04-22, 502 days, 415 single-names, **98.6%** non-null | **Reliable** ✅ |
| **IV skew** | same file: `otm_put_iv`, `otm_call_iv`, `skew`/`skew_20d` (vendor IV) | same window, 79.9% non-null | **Suspect** — SP-4 flagged this file's IV (SPY iv30 vs VIX corr 0.375). Use as *soft secondary* only |
| **Sector/broad ETFs** | `data/master/prices.parquet` columns: SPY/QQQ/IWM/DIA + 11 SPDRs (XLK/XLF/XLE/XLV/XLI/XLP/XLY/XLU/XLB/XLRE/XLC) | ~10y daily closes (2016→2026; XLC from 2018) | **Reliable** ✅ |
| **ticker→sector map** | none in DB (`sector` 100% NULL); precedent `S_industry_momentum_moskowitz._SECTOR_MAP` (~160 tickers) | n/a | **Must build** (static curated GICS) |
| **Live sentiment** | `ticker_sentiment_daily` + `data/master/sentiment.parquet` | only ~1 week (since 2026-05-20); **not in backtest aux-data path** | **Not backtestable as-is** ❌ |
| **Historical news** | Alpaca news API (Benzinga) — verified returns AAPL articles from **2018 and 2020** | deep | **Backfillable** ✅ (news-only) |

**Key constraints carried into the design:**
- The options aux-loader **forward-fills the last available slice** for dates after 2026-04-22.
  → ① backtest is **bounded to `end_date ≤ 2026-04-22`** to avoid trading on stale flow.
- The 2024-04-22 → 2026-04-22 options window is **almost entirely calm-regime** → ① cannot
  validate HIGH_VOL/CRISIS robustness; reported as an explicit caveat, not hidden.

## 5. Architecture — shared confirmation framework

New package **`src/strategies/confirmation/`** of pure, deterministic, I/O-free functions:

- `options_flow.py` → `confirm(direction: str, opts_row: dict, params: dict) -> tuple[bool, float]`
  - LONG passes when PCR is low (call demand); SHORT passes when PCR is high (put demand).
  - Returns `(passes, score)` where `score ∈ [-1, 1]` blends PCR (primary) and skew (soft, downweighted).
- `sector_flow.py` → `confirm(direction, ticker, prices, sector_map, as_of, params) -> tuple[bool, float]`
  - LONG passes when the ticker's sector SPDR **and** the broad market are in an aligned uptrend
    (ETF above its MA / positive trailing return); SHORT mirrors.
- `news_flow.py` → `score(sentiment_row: dict, params: dict) -> float` (signed sentiment used by ③).
- `sector_map.py` → static `TICKER_SECTOR: dict[str,str]` (GICS) + `SECTOR_ETF: dict[str,str]`
  (e.g. `'Technology' -> 'XLK'`). Extends the Moskowitz precedent and adds the sector→ETF leg.

Pure functions ⇒ unit-testable in isolation (TDD), reusable, and **parity-safe** between
backtest and a future live gate. Each strategy's `generate_signals()` computes its base signal,
then calls the relevant `confirm()`/`score()` to filter or rank.

## 6. Strategy ① — `S_options_flow_confirmed_momentum`

- **instrument_class:** `equity`. **active_in_regimes:** LOW_VOL, TRANSITIONING (calm data window).
- **Universe:** options-eligible single-names present in the enriched aggregates (~415).
- **Base:** cross-sectional momentum — rank by ~63-day return (skip last 5d). Candidate LONGs =
  top decile, candidate SHORTs = bottom decile.
- **Corroboration:** keep a candidate only if `options_flow.confirm(direction, aux_data['options'][t])`
  passes. PCR is the primary gate; skew nudges the score but cannot by itself pass/fail.
- **Sizing/stops:** standard `compute_stops_and_targets`; position_size_pct via equal-weight × regime scale.
- **Backtest:** `--start-date 2024-04-22 --end-date 2026-04-22`.

## 7. Strategy ② — `S_sector_flow_confirmed_momentum`

- **instrument_class:** `equity`. **active_in_regimes:** LOW_VOL, TRANSITIONING, HIGH_VOL.
- **Universe:** active equities present in `sector_map.TICKER_SECTOR`.
- **Two modes (both long & short):**
  1. **Confirmation mode (default):** base momentum signal per stock, kept only when
     `sector_flow.confirm()` agrees (LONG needs aligned up-sector + up-market; SHORT needs aligned down-sector + down-market).
  2. **Sector-basket mode:** rank sectors by ETF trend; **LONG the constituents of the strongest
     sector(s), SHORT the constituents of the weakest sector(s)** (symmetric — operator requirement).
     `MAX_SIGNALS` caps basket breadth.
- **Backtest:** full ~10y window (default start; end open).

## 8. News-history backfill (prerequisite for ③)

Standalone data sub-project, runnable in the background:

1. **Fetch:** page Alpaca news (`alpaca data news --symbols <chunk> --start <ISO> --end <ISO>
   --include-content --exclude-contentless --limit ... --page-token ...`) across the active
   universe, target depth 2018+ (cap if compute runs long; log any cap — no silent truncation).
2. **Score:** FinBERT via the local service (`finbert-sentiment.service`, `127.0.0.1:7872`,
   `{text}`-in / label+score-out), reusing `src/ingestion/alpaca_news.py` scoring; batched.
3. **Aggregate:** per `(ticker, date)` keyed on article `published_at`; a date's sentiment is
   **available T+1** (no lookahead). Multi-ticker articles expand to each symbol.
4. **Persist:** append historical rows to the sentiment store (`sentiment.parquet` /
   `ticker_sentiment_daily`) — **append-only**, never overwriting live rows; idempotent on `(ticker,date)`.
5. **Raw archive:** optionally append fetched articles to `market_news` (append-only) for audit.

## 9. Strategy ③ — `S_news_sentiment_long_short`

- **instrument_class:** `equity`. Depends on §8 backfill **and** a new aux-data hook.
- **aux-data extension:** add a `sentiment` panel to `src/strategies/aux_data_loader.py` that
  point-in-time slices the historical sentiment store (same `date <= as_of` discipline as options),
  exposing per-ticker `news_mean_score`, `news_count_24h`, FinBERT pos/neu/neg, etc.
- **Signal:** cross-sectional — LONG strongly-positive sentiment names with sufficient article
  volume, SHORT strongly-negative; trailing-window smoothing to reduce single-headline noise.
- **Backtest:** over the backfilled depth. **Documented caveat:** news-only signal; live adds social.

## 10. Validation protocol — proving "functional & profitable"

- **TDD** for every module (confirmation functions, sector map, backfill, aux-data extension).
- **`validate_strategy.py`** must pass for each `.py` (signature, empty-frame safety, Signal field types, canonical regimes).
- **`unified_backtest`** per strategy → Sharpe / Sortino / Calmar / MaxDD / return / hit-rate / PF /
  trade-count, **plus per-regime breakdown**.
- **Lift test (① & ②):** run base-only vs base+corroboration. The corroboration must **raise
  Sharpe or cut MaxDD** — otherwise it is not adding alpha and we do not ship it as a "corroboration" strategy.
- **Promotion gate:** equity `PROMOTION_THRESHOLDS` = **Sharpe ≥ 0.5, MaxDD ≤ 0.20** (`lifecycle.py`).
- **Operator-gated promotion:** passing strategies are registered (`_IMPL_MAP` + manifest) as
  `candidate`; the operator receives the metrics and decides promotion. Failing strategies are
  iterated or shelved — **no unproven alpha reaches the live stack**.

## 11. Sequencing & decomposition

Each work item gets its own implementation plan (separate spec→plan→build cycle), executed in order;
the backfill runs in the background early.

1. **Foundation:** `src/strategies/confirmation/` scaffold + `sector_map.py` + unit tests.
2. **① options-flow** (data ready, fastest) → backtest → lift test → register `candidate`.
3. **② sector-flow** (both modes, long & short) → backtest → lift test → register `candidate`.
4. **News backfill** (background, kicked off early).
5. **③ sentiment** — aux-data extension + strategy → backtest → register `candidate`.

## 12. Risks & honest caveats

- **①:** calm-only 2-year window; cannot prove tail-regime robustness. IV-skew leg is low-fidelity
  (PCR carries the gate). Forward-fill bound enforced at `end_date`.
- **②:** static sector map drifts as constituents change (acceptable for a backtest; refresh policy noted).
  Equal-weight sector returns (no point-in-time market cap) per the Moskowitz precedent.
- **③:** backtested signal (news-only) is structurally different from the live signal (news+social);
  backfill compute is the long pole; rate limits on the news API may extend it.
- **Cross-cutting:** the 2-year overlap of options data and the ~10y sector/sentiment windows means
  the three strategies are validated over different spans — comparisons are within-strategy, not across.

## 13. Open items (non-blocking)

- Exact base-momentum lookback/decile thresholds — tuned during implementation, reported in backtest.
- Backfill depth cap — set empirically against news-API throughput; any cap is logged.
- Whether ② basket mode and confirmation mode ship as one strategy with a param or two strategies —
  resolved in the ② plan (leaning one strategy, mode param).
