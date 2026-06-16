# Strategy-Blueprint Source Expansion — for the research pipeline

**Date:** 2026-06-16
**Goal:** raise the research pipeline's hit rate by ingesting sites that publish EXPLICIT, reproducible rule-based strategy blueprints (entry/exit/params + author backtests), beyond academic papers.
**Method:** deep-research workflow (fan-out search → fetch → 3-vote adversarial verification → synthesis); 61 primary-source-verified claims. ✅ = primary-source verified by the workflow; ◽ = domain-knowledge candidate (verify before wiring).
**Fit** = suitability for our backtest engine: DAILY OHLC for ~S&P-500 equities + liquid ETFs, no intraday/futures/options-chain.

## Tier 1 — ingest first (free, explicit rules, daily equity/ETF)

1. **✅ Quantocracy** — `quantocracy.com` — **the single highest-leverage ingestion point.** A daily curated "Quant Mashup" that AGGREGATES the quant-blog ecosystem (Allocate Smartly, Quantpedia, Robot Wealth, Alvarez, Quantitativo, …). **Has RSS:** `https://feeds.feedburner.com/Quantocracy` + `https://quantocracy.com/feed/`. **Ingest:** subscribe to the RSS in the paper/source-expansion pipeline → you passively receive every new blueprint post across all member blogs. Fit: **HIGH** (hub, not a source itself).

2. **✅ awesome-systematic-trading (GitHub)** — `github.com/paperswithbacktest/awesome-systematic-trading` — README table (`Title | Sharpe | Volatility | Rebalancing | Implementation | Source`) linking **~80 complete QuantConnect `.py` strategy files** in `./static/strategies/` with explicit params (e.g. time-series-momentum: `period=12*21`, `targeted_volatility=0.10`), each tied to a paper. **Ingest:** clone the repo / fetch raw `.py` files — already coded, daily, equity/ETF. Fit: **HIGH**.

3. **✅ TuringTrader** — `turingtrader.com` (+ `.org`, AGPL-3.0 engine `github.com/fbertram/TuringTrader`) — per-strategy pages with FULL numeric "Strategy Rules" freely readable (no login), e.g. Faber Ivy (avg 3/6/12-mo momentum, top-3, monthly ETF), Connors TPS (2-period RSI<25 two days, scale-ins, S&P-500 ETF, daily). `BooksAndPubsV2/` has compilable C# (e.g. Keller BAA on SPY/QQQ/IWM/VGK/EWJ/VWO/VNQ/DBC/GLD/TLT/HYG/LQD). End-of-day, ETF. **Ingest:** per-strategy pages (crawlable) + GitHub. Fit: **HIGH**.

4. **✅ Quantpedia** — `quantpedia.com` — 900–1200+ strategies; **freemium**: free "trial" strategies are fully readable (e.g. `/strategies/asset-class-trend-following/` verbatim: "Hold each asset-class ETF only when over its 10-month SMA, else cash"), premium for the rest + QuantConnect code. Bi-weekly additions. **Ingest:** crawl the free `/strategies/` pages + the free blog; filter to daily equity/ETF (many entries need data we lack). Fit: **HIGH (filter)**.

5. **✅ Alvarez Quant Trading** — `alvarezquanttrading.com` — Cesar Alvarez (ex-Connors). Representative page verified to publish the FULL rule set free (Entry: Close>100-MA & Close<5-MA & 3 lower lows, limit buy at prevClose−0.5·ATR(10); Exit: Close>prev close → sell next open; Russell universe). Daily US equity, mean-reversion. **Ingest:** RSS. Fit: **HIGH**.

6. **◽ Quantified Strategies** — `quantifiedstrategies.com` — Oddmund Groette. The closest oxfordstrat analog: *hundreds* of free posts with explicit entry/exit + params + backtest stats, often code; heavy SPY/QQQ/sector/ETF, daily. (Feeds Quantocracy.) **Ingest:** RSS / per-strategy crawl. Fit: **HIGH** (spot-verify a page before bulk-crawl).

7. **◽ Robot Wealth** — `robotwealth.com` — Kris Longmore. Free blog, explicit edges + code, daily equity/ETF (+crypto). (Feeds Quantocracy.) **Ingest:** RSS. Fit: **HIGH**.

## Tier 2 — useful, but filter / partial

8. **✅ QuantConnect Strategy Library** — `quantconnect.com/strategies/` — "top community strategies that update weekly," 1,200+ shared in forums, full source (LEAN open-source). **Ingest:** strategy pages + forum; filter to daily equity. Fit: **MEDIUM-HIGH**.
9. **✅ Allocate Smartly** — `allocatesmartly.com/list-of-strategies/` — **CORRECTION to first pass: rules are PAYWALLED.** Only the *names + authors* of ~80 named TAA strategies are free (rules/params/performance behind ~$399/yr). Value = a free *index of named strategies* whose rules you then reconstruct from the cited source papers/books. Monthly ETF TAA. Fit: **MEDIUM** (idea index, not free blueprints).
10. **◽ Meb Faber** (`mebfaber.com`), **System Trader Success** (`systemtradersuccess.com`, more futures), **CXO Advisory** (strategy *reviews/ratings* — oxfordstrat-style validation), **PriceActionLab**, **Logical-Invest / TrendXplorer**, **QuantStart**, and quant **Substacks** (Quantitativo, Quant Galore). Fit: **MEDIUM** — verify free-vs-paywall + daily-equity fit per source.

## Non-crawlable but blueprint-dense (manual extraction pass)
Connors *"Short Term Trading Strategies That Work"*, Clenow *"Stocks on the Move"*, Bensdorp *"Automated Stock Trading Systems"*, Carver *"Systematic Trading"* — each is essentially an oxfordstrat in book form (explicit daily-equity rules + params).

## Recommended wiring into the pipeline
1. **Add Quantocracy RSS** (`feeds.feedburner.com/Quantocracy`) to the Sunday `paper-expansion` source set — one feed covers most Tier-1 blogs passively. Highest ROI, lowest effort.
2. **Bulk-import awesome-systematic-trading** `./static/strategies/*.py` — ~80 already-coded daily strategies; translate the rule into a `BaseStrategy` (same pattern as the oxfordstrat build).
3. **Crawl TuringTrader + Quantpedia-free + Quantified Strategies** per-strategy pages on a cadence; route each through PaperHunter-style extraction → StrategyCoder, filtering to daily equity/ETF.
4. **Treat Allocate Smartly's list as an idea index** (names → cited papers), not a free-rules source.
