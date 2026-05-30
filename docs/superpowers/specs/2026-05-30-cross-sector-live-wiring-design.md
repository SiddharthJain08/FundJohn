# Cross-Sector Corroboration — Live Wiring + Promotion (2026-05-30)

Promote the two surviving cross-sector strategies (PR #12 / branch
`feat/cross-sector-corroboration-impl`) from `candidate` to `live`, after closing
the one live-execution gap that blocks ③.

## Strategies

| id | backtest | live status before this work |
|----|----------|------------------------------|
| ① `S_options_flow_confirmed_momentum` | Sharpe 1.91 / DD 11.5% (TRANSITIONING-only) | wiring OK; pc_ratio parity verified clean |
| ③ `S_news_sentiment_long_short` | Sharpe 0.996 / DD 11.8% (all-regime) | **broken** — engine never builds `aux['sentiment']`; live news breadth ~½ of backtest |

Both pass the equity promotion gate (`min_sharpe 0.5`, `max_drawdown 0.20`).

## ① pc_ratio parity (verified, no code change)

Backtest sourced `pc_ratio` from `options_aggregates_enriched.put_call_vol_ratio`;
live computes it from raw `options_eod` contract volume (`engine.py:418-428`). Spot
check on 2026-05-29 (SPY 1.03 vs backtest median 1.01, AMZN 0.35 vs 0.39, AAPL/MSFT/
NVDA/TSLA within the backtest p10–p90 envelope) → same metric, same distribution.
The 0.85/1.05 gates behave identically. ① is TRANSITIONING-only, so it stays inert
until the next TRANSITIONING regime regardless.

## ③ fix — two parts

### A. Wire `aux['sentiment']` into the live engine

`engine.load_aux_data()` builds `options/financials/insider/macro` but no
`sentiment` key, so the strategy receives `{}` and returns no signals.

True source parity is **backtest `news_*` ↔ live `alpaca_news_*`**: the backtest
`sentiment.parquet` `news_*` columns were built from Alpaca news via the *identical*
FinBERT scorer; live, that same signal lands in `ticker_sentiment_daily.alpaca_news_*`
(the legacy `news_*` columns are a dead RSS source, ~0% covered since 2026-05-22). So
the live read maps `alpaca_news_*` → the `news_*` dict keys the strategy/scorer expect.
**Do NOT coalesce with legacy `news_*`** (different source/scorer → breaks parity).

New pure module `src/execution/sentiment_aux.py:build_sentiment_aux(rows, as_of,
max_age_days=7)` replicates `aux_data_loader._sentiment_day_slice` semantics exactly:

- point-in-time: `date <= as_of`
- forward-fill: latest **news-bearing** row per ticker (`alpaca_news_count_24h > 0`)
  — critical, because live writes a `count=0` row for every symbol every day, which
  would otherwise shadow an older news-bearing row and defeat forward-fill
- 7-day staleness cap (mirror `SENTIMENT_MAX_AGE_DAYS = 7`)
- remap: `alpaca_news_count_24h→news_count_24h`, `alpaca_news_mean_score→news_mean_score`,
  `alpaca_news_finbert_{pos,neu,neg}→news_finbert_{pos,neu,neg}`

`engine._sentiment_slice(universe)` does a thin range-fetch (universe, last 7d,
`count>0`) and delegates to the pure function. Additive — touches no other aux key.

### B. Close the live news breadth gap

Live `alpaca_news._fetch_news_chunk` takes only the first page (`--limit 50`) and
ignores `next_page_token`, so busy 50-symbol chunks drop articles → ~80 tickers/day
with news vs the backfill's ~187 (the backfill paginates, `_fetch_window`). Fix:
paginate `_fetch_news_chunk` through `next_page_token` (max-pages safety cap),
mirroring the backfill. Strictly additive to coverage; per-page retry semantics
preserved.

## Sequence

1. TDD breadth pagination (`alpaca_news.py`).
2. TDD `build_sentiment_aux` (pure) + wire `_sentiment_slice` into `engine.load_aux_data`.
3. Verify: dry-run `engine.load_aux_data(universe)` → `aux['sentiment']` breadth;
   confirm ③ generates signals; run paginated ingest on a sample → more breadth.
4. Promote ① + ③ `candidate → live` via lifecycle.
5. Deploy: commit → merge to `main` → push → restart johnbot → verify next cycle.

## Out of scope

Sector-flow strategy ② (disproven, not registered). Social sentiment in the live
signal (the implemented scorer is news-only on both sides — no divergence).
