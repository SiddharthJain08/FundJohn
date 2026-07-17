# Data-Provider Overhaul — Multi-Sub-Project Handoff

**Created:** 2026-05-21
**Purpose:** Pass to a fresh Claude Code session to brainstorm + design SP-2, SP-3, SP-4, or SP-5 without re-doing the SP-0 audit.
**Parent program:** Operator purchased Alpaca Algo Trader Plus (AAT Plus), cancelled Polygon Options Starter. Massive S3 (Polygon-affiliated) lost with it. Goal: Alpaca + FMP top-tier providers, minimize latency, expand from S&P 500 to broader Alpaca universe relevant to current strategies, broaden weekly research implementability to new asset classes.

**Read this first**, then `/root/openclaw/docs/superpowers/specs/2026-05-21-sp1-provider-cutover-design.md` for context on what SP-1 shipped, then pick a sub-project below to brainstorm.

---

## 0. Decomposition Map

```
SP-0  Capability anchor (one-shot, not a spec — done)
SP-1  Daily pipeline provider swap                                ← in flight / shipping
SP-2  Universe expansion (S&P 500 → Alpaca tradable universe)
SP-3  Asset-class expansion (equities → equities + options + crypto + ...)
SP-4  Weekly research uplift (PaperHunter + StrategyCoder learn new scope)
SP-5  Observability + cutover hardening (cross-cutting)

Dependency order:
SP-0 (done) → SP-1 (in flight) → SP-2 (universe) → SP-3 (asset class)
                              └── SP-4 (research) — depends on SP-2 + SP-3
                              SP-5 (lands alongside SP-1)
```

---

## 1. Anchored Facts (from SP-0 audit + live probes)

### AAT Plus capabilities (verified 2026-05-21)
- **Greeks populate on actively-traded contracts**: SPY 30-DTE ATM = full set; AAPL 30-DTE ATM = full set; GME near-ATM = full set.
- **Zero-greek strikes are bounded** to: 0-DTE/expired contracts, OR deep-ITM with zero recent volume — exactly the contracts a sane strategy wouldn't trade.
- **Alpaca CLI data subcommands available**: bars, quotes, trades, snapshot, latest-bar/bars/quote/quotes/trade/trades, auction, auctions, news, fixed-income, logo, meta, forex, crypto, crypto-orderbook, screener, corporate-actions, option (chain + bars + quotes + trades + latest-*).
- **No WebSocket** in the CLI — streaming requires adding an alpaca-py client separately (SP-5 candidate).
- Rate limits at AAT Plus: ~10k/min for market data; effectively no overage concerns at our universe size.

### FMP tier
- **Starter (~300 req/min)**, hardcoded in `preferences.json`.
- Works: `/stable/quote`, `/api/v3/historical-price-full/{symbol}`, `/api/v3/earnings-surprises/{symbol}`, `/api/v3/insider-trading`, `/stable/income-statement,balance-sheet-statement,cash-flow-statement,key-metrics,ratios`, `/api/v3/available-traded/list`, `/stable/profile`, `/stable/stock-peers`, `/stable/price-target-consensus`, `/stable/historical-price-eod/full`.
- **403 on Starter**: bulk `earning_calendar` (forward-looking). Per-ticker historical endpoint works.
- **Unused on Starter (expansion candidates)**: `/sector-performance`, `/economic-calendar`, forex, commodities (oil/nat-gas/silver/gold), detailed insider holdings, `/price-target-consensus`.
- **No tier env var** — tier change requires `preferences.json` edit.

### Polygon status
- Options Starter cancelled. Free tier still functional but operator wants clean break.
- Treated as fully revoked. SP-1 strips polygon.py, yahoo.py, massive_client.py, massive_ws.py, agent/tools/mcp/polygon.js, agent/tools/mcp/yahoo.js, pipeline/backfillers/polygon.py.

### yfinance bounded scope
- After SP-1 ships: yfinance is ALLOWED only in `src/ingestion/cboe_vol_indices.py`.
- CI lint enforces. Surface: `get_vix()`, `get_vvix()`, `get_vix3m()`, `get_vix9d()`, possibly `get_forward_earnings_calendar()` if FMP Starter probe fails for forward earnings.

### Historical options EOD
- Massive (Polygon-affiliated) is dead.
- Replaced by: daily EOD self-archive from Alpaca chain (`pipeline.backfillers.alpaca_options`) + one-shot cutover-gap backfill via per-contract `option bars`.

### Existing pipeline (post-SP-1)
- Daily cycle Mon–Fri 10:00 ET: `collect → sentiment → signals → ic_gate → handoff → trade → alpaca → reconcile → report → pyportfolioopt_shadow → health`.
- `trade` stage = `regime_blended_sizer_live.py` (Sonnet 4.6 confirmer per ticker).
- Intraday HMM `*/5 9-19 * * 1-5` triggers `redeploy_pipeline.py` on regime transitions (hysteresis + confidence + cooldown gated).
- 51 live + 51 candidate strategies in `src/strategies/manifest.json`.
- Universe: S&P 500 (untouched in SP-1).
- Asset classes traded: equities + options (gated). Crypto + commodities = no strategies yet.

### Master parquets (append-only — CLAUDE.md core invariant)
- `prices.parquet`, `options_eod.parquet`, `financials.parquet`, `macro.parquet`, `insider.parquet`, `earnings.parquet`, `prices_30m.parquet`, `historical_regimes.parquet`, `intraday_features.parquet`, `vol_indices.parquet`, `corporate_actions.parquet`.
- Same rule for canonical Postgres tables: `execution_signals`, `signal_pnl`, `alpaca_submissions`, `data_coverage`, `data_columns`, `analyses`, `verdict_cache`, `trades`, `checkpoints`, `market_regime`, `ticker_sentiment_daily`, `mastermind_chat_*`, `research_corpus`, `research_candidates`, `strategy_*`.
- New columns/tables OK. Deletes/truncates NOT OK.

---

## 2. SP-2 — Universe Expansion

### Goal
Expand the tradeable universe from S&P 500 (~500 tickers) to the relevant subset of the Alpaca tradable universe (~8,000+ US equities), with per-strategy universe-slice contracts so each strategy declares what slice it operates on.

### Current state (post-SP-1)
- `alpaca_tradable_universe` table exists (migration 092). Refreshed daily via `alpaca_screener.js`.
- `universe_config` table holds the active subset (currently S&P 500 filtered).
- Strategies have implicit universes (hardcoded ticker lists, or "S&P 500 minus filter").
- `src/ingestion/pipeline.py:628-666` syncs full FMP `/available-traded/list` to a metadata table.

### Likely scope
1. **Filter contracts**: define per-strategy universe declarations.
   - Liquidity floor: avg daily volume / dollar volume threshold.
   - Options-eligible: only options-trading underlyings (Alpaca corp-actions / asset list flag).
   - Market-cap bands: micro/small/mid/large/mega.
   - Sector caps: max N tickers per GICS sector.
   - Exclusion sets: penny stocks, OTC, ADRs (per strategy).
2. **`StrategyRecord` schema**: add `universe_slice: dict` field (well-defined contract).
3. **Universe-resolver service**: given a universe_slice spec, return the concrete ticker list for "today".
4. **Backtest engine**: per-day universe (point-in-time, not forward-looking — needed to avoid look-ahead bias in backtests).
5. **Migration**: existing 51 live + 51 candidate strategies migrated to explicit universe_slice declarations (default = "sp500" for now to preserve behavior).
6. **Doctor + dashboard updates**: per-strategy universe size visible, sanity checks (no zero-ticker universes).

### Considerations / known constraints
- Existing `alpaca_tradable_universe` is broker-broad (8k+); we want sensible filtering, not all of it.
- Backtest data may not cover the full broader universe — coverage gaps in `prices.parquet` for non-S&P 500 tickers since pre-cutover. Coverage backfill is a sub-task.
- Data API rate limits + cost: more tickers = more API calls per cycle. Need to verify cycle wall time stays under Alpaca rate cap.
- 30-min options archive (SP-1) becomes 60-90 min if universe doubles — soft-budget tuning needed.

### Decisions to brainstorm
- Default universe slice for existing strategies: stay S&P 500, or move to "S&P 500 + Russell 1000"?
- Liquidity floor: by ADV ($), shares, or both?
- Schema: enum-based slice tags ('sp500', 'r1000', 'r3000') or fully-declarative dict?
- Backtest backfill: how far back, what coverage?
- Implementation phasing: per-strategy gradual rollout (one strategy at a time on broader universe), or atomic flip?

### Useful files to read first
- `src/strategies/lifecycle.py` (StrategyRecord dataclass — adding fields requires updating from_manifest/to_dict)
- `src/ingestion/pipeline.py:628-666` (universe sync to FMP)
- `src/database/migrations/092_alpaca_tradable_universe.sql`
- `src/pipeline/alpaca_screener.js`
- Any backtest scripts under `src/backtest/`

### Memories to read
- `feedback_lifecycle_silent_strip.md` (StrategyRecord field-strip pitfall)
- `project_alpaca_cli_integration.md` (tradable universe table origin)

---

## 3. SP-3 — Asset-Class Expansion

### Goal
Broaden the tradeable asset classes from equities + options (current) to include crypto (24/7) and commodities/futures (via Alpaca-tradeable ETPs and any direct futures products). New strategy archetypes become possible (delta-hedged volatility, crypto carry, commodity momentum, ETP arbitrage, calendar spreads).

### Current state
- Strategies are equity-and-options-only.
- `StrategyRecord` has no `asset_class` field.
- Executor (`alpaca_executor.py`) supports equity orders; options gated; crypto + futures untested in our codepath.
- Backtest engine assumes equity bars (`prices.parquet`); options use `options_eod.parquet`. No crypto/commodity data tables.

### Likely scope
1. **`asset_class` field** added to StrategyRecord (`equity`, `option`, `crypto`, `etp`, `futures`). Threaded through manifest, lifecycle, sizer, executor.
2. **Per-asset-class sizing**: crypto trades 24/7 with very different vol regimes than equities — fixed % cap may not be right. Greeks-aware options sizing (delta-equivalent dollar exposure rather than notional). Need per-asset-class risk treatment in `regime_blended_sizer_live.py`.
3. **Executor per-asset routing**: equity + option (existing), crypto via `alpaca crypto-perp` (if perpetuals are in scope) or spot, commodities via Alpaca asset list (likely ETPs only; verify by `alpaca asset list --asset-class us_equity` filtering).
4. **New data tables**: `crypto_prices.parquet`, `crypto_orderbook.parquet`. New ingestion modules: `src/ingestion/alpaca_crypto.py`.
5. **Backtest engine multi-asset**: time alignment differs (24/7 crypto vs RTH equities), execution cost differs (crypto wider spreads), vol regime model may need separate HMM or shared HMM with asset-class feature.
6. **Lifecycle promotion guards per asset class**: Sharpe ≥ 0.5 + MaxDD ≤ 20% may not be right for crypto — likely need separate thresholds.
7. **Doctor checks**: crypto market is always open, so "is market open" check needs asset-class awareness.

### Considerations / known constraints
- Alpaca asset taxonomy: verify exact tradeable list (`alpaca asset list --status active --asset-class crypto` etc.).
- Commodities: Alpaca offers commodity ETFs (GLD, USO, etc.) as equities; direct futures are limited. Likely commodity coverage = ETPs in `equity` asset class.
- Crypto: Alpaca supports spot (BTC, ETH, SOL, etc.) and perps (crypto-perp subcommand).
- Tax accounting + reporting differ per asset class (1099 vs schedule D etc.) — likely out of scope but flag.
- The "minimize data streaming latency" goal interacts with this — crypto streaming WS is a clear use case (SP-5 dependency).

### Decisions to brainstorm
- Asset-class taxonomy: enum or freeform?
- Per-asset-class sizer plug-in pattern or single sizer with asset-class branches?
- Crypto in MVP or deferred to a SP-3.1?
- Backtest engine: refactor or fork per asset class?
- Lifecycle promotion guards: per-asset-class thresholds in YAML?

### Useful files to read first
- `src/strategies/lifecycle.py` (asset_class field threading)
- `src/execution/regime_blended_sizer_live.py` (per-asset-class sizing)
- `src/execution/alpaca_executor.py` (executor routing)
- `src/backtest/unified_backtest.py` (multi-asset engine)
- Probe: `/root/go/bin/alpaca asset list --asset-class crypto --status active`
- Probe: `/root/go/bin/alpaca data crypto bars --symbols BTC/USD --timeframe 1Day --start 2026-05-01`

### Memories to read
- `project_regime_blended_sizer.md` (sizer architecture for delta extension)
- `feedback_lifecycle_silent_strip.md` (StrategyRecord field-strip pitfall)
- `feedback_silent_failure_pattern.md` (HMM regime + asset class — if shared model breaks, all asset classes affected)

---

## 4. SP-4 — Weekly Research Uplift

### Goal
The Saturday research workflow (corpus curator + PaperHunter swarm + StrategyCoder + MasterMind reviewer) learns that the broader universe (SP-2) and new asset classes (SP-3) are in scope. PaperHunter accepts options/crypto/commodity papers as implementable. StrategyCoder has templates for new archetypes. MasterMind corpus filters accept the broader implementability.

### Current state
- Saturday 10:00 ET: `openclaw-saturday-brain.service` runs paper ingestion + PaperHunter swarm + MasterMind corpus curation.
- Saturday 18:00 ET: `openclaw-strategy-review.service` (Opus 4.7 1M comprehensive per-strategy review).
- Saturday 19:00 ET: `openclaw-position-recs.service` (deterministic, no LLM).
- Sunday 08:00 ET: `openclaw-paper-expansion.service` (Opus + WebSearch open-ended source discovery).
- PaperHunter swarm has 4 rejection gates: abstract blueprint-signal regex, implementability score, data availability, backtest pass.
- arXiv categories: q-fin.ST/PM/TR/CP/GN/RM + cs.LG/AI/CL + stat.ML.
- Strategies stored at `src/strategies/implementations/S_*.py` with `.requirements.json` pairs.
- Backtest thresholds: Sharpe ≥ 0.5, MaxDD ≤ 20% for CANDIDATE → LIVE.

### Likely scope
1. **PaperHunter implementability gate**: update to accept options/crypto/commodity strategies. Currently rejects anything not equity-momentum-flavored?
2. **StrategyCoder templates**: new templates per asset class (delta-neutral options, crypto carry, commodity momentum, etc.).
3. **MasterMind corpus filters**: update mode=corpus prompt to recognize broader asset-class candidates.
4. **arXiv category expansion**: add q-fin.PR (Pricing of Securities — covers options pricing, derivatives), and potentially math.PR (probability — for vol models).
5. **Per-asset-class lifecycle thresholds**: SP-3 introduces them; SP-4 wires them into the StrategyCoder + comprehensive-review.
6. **Backtest fidelity**: SP-3 multi-asset engine; SP-4 makes sure PaperHunter -> StrategyCoder -> backtest path works end-to-end for new asset classes.
7. **Comprehensive review (Sat 18:00)**: per-strategy lifetime review with 30-day OUE histogram — verify it works for non-equity strategies.
8. **Position-recs (Sat 19:00)**: sizing recs per strategy — needs asset-class-aware logic.

### Considerations / known constraints
- The Saturday brain is a 4-6 hour job already. Adding asset-class branches may extend it.
- Cost: Opus 4.7 1M passes are expensive ($8 budget per call). Broader corpus = more passes.
- 51 live + 51 candidate strategies (current) — adding new asset classes brings dozens more candidates.
- Universe expansion (SP-2) interacts: per-strategy universe slice means PaperHunter has to evaluate "does this paper fit a slice we can build, given our universe?".

### Decisions to brainstorm
- Prompt-level vs structural changes to PaperHunter (prompt is cheaper, structural is more robust)?
- Add new arXiv categories or stay focused?
- New strategy template repo / scaffolding tool?
- Backtest fidelity bumps for options strategies (Greeks-aware backtest)?
- Should Sunday paper-expansion explicitly target new asset classes?

### Useful files to read first
- `src/agent/graphs/paperhunter.js` (Send-based fan-out)
- `src/agent/curators/mastermind.js` (corpus mode)
- `src/agent/curators/comprehensive_review.js` (Sat 18:00)
- `src/agent/curators/position_recommender.js` (Sat 19:00)
- `src/agent/curators/saturday_brain.js`
- `src/strategies/implementations/_greeks_filter.py` (post-SP-1)
- `src/backtest/unified_backtest.py`
- `src/agent/prompts/subagents/paperhunter.md` (or wherever the prompt lives)

### Memories to read
- `project_d1_sentiment.md` (recent pipeline extension pattern — useful template)
- `project_e1_langgraph_orchestrator.md` (orchestration patterns)
- `feedback_d1_schema_drift.md` (pre-flight schema checks before dispatching subagents)
- `project_opus_corpus_curator.md` (corpus curation design)

---

## 5. SP-5 — Observability + Cutover Hardening

### Goal
Cross-cutting infrastructure: latency SLOs (currently untracked), expanded `data_provider_health` with per-endpoint percentiles, parity/regression harness, formal rollback gates, dashboard "Data Health" tile expanded into a full provider observability surface. Also: add a real-time streaming layer (Alpaca CLI is REST-only; for the "minimize latency" goal we need WebSocket).

### Current state (post-SP-1)
- SP-1 added: `data_provider_health` table (rolling 24h counters), Data Health tile on :7870 dashboard, doctor preflight expanded with AAT Plus tier check + options archive freshness + vol indices freshness, soak mode (tightened alerts for first 7 days).
- Discord: `#data-alerts` for warnings, `#botjohn-log` for cycle-critical.
- No latency tracking — only success/error counts.
- No WebSocket streaming; all market data via REST polling on 10:00 ET cycle + 5min intraday HMM refit.

### Likely scope
1. **Latency SLOs**: per-provider, per-endpoint p50/p95/p99 wall time. Histograms stored in Postgres. Daily digest includes p95 movement vs 7-day baseline.
2. **Provider-comparison harness**: when adding a new provider (e.g., Tiingo for vol indices if yfinance becomes a problem), parity-shadow harness reusable.
3. **Streaming WebSocket layer**: add `alpaca-py` WebSocket consumer (Python long-running daemon) that subscribes to live quotes + trades for the active universe and publishes to Redis pub/sub. Strategies that want intraday tick-level data subscribe to Redis topics rather than polling. Sub-second latency vs the current minute-cadence.
4. **Per-cycle wall-time budget tracking**: each pipeline stage has expected wall-time; alerts on >2σ deviations.
5. **Data Health tile expanded**: per-endpoint latency, error breakdown by HTTP code, last-error inline.
6. **Doctor exit code mapping documented**: 0/1/2 + structured JSON, currently scattered.
7. **Rollback gates formalized**: each new feature ships with a kill-switch env var documented in `docs/runbooks/rollback-gates.md`.
8. **Cost tracking**: per-provider monthly spend + per-cycle API call attribution.

### Considerations / known constraints
- WebSocket adds a long-running process — needs systemd unit, restart-on-failure, lag-detection.
- Streaming subscriber for ~200 tickers (post-SP-2: 1000+) — bandwidth + memory.
- Redis pub/sub vs Redis Streams: streams is durable + replay-capable but more complex. Pub/sub is fire-and-forget.
- Latency tracking adds DB writes per API call — overhead. Use sampling (1-in-100 calls) for high-volume providers.

### Decisions to brainstorm
- Streaming: pub/sub or streams?
- Latency sampling rate per provider?
- Cost tracking: simple counters or full per-call attribution?
- Should SP-5 ship alongside SP-1 (originally proposed) or after SP-1 settles?
- Provider-comparison harness: how reusable can we make it?

### Useful files to read first
- `src/maintenance/doctor.py` (current preflight)
- `src/channels/dashboard/server.js` (Data Health tile post-SP-1)
- `src/database/migrations/110_data_provider_health.sql` (post-SP-1)
- `src/execution/pipeline_orchestrator.py` (stage wall-time tracking surface)

### Memories to read
- `feedback_silent_failure_pattern.md` (the whole reason we're hardening)
- `feedback_lifecycle_silent_strip.md` (same)
- `project_alpaca_tier3.md` (doctor + exit code discipline)

---

## 6. Cross-Cutting Notes for Any Sub-Project

### Always do first
1. Re-read `/root/CLAUDE.md` + `/root/openclaw/CLAUDE.md` + memory index at `/root/.claude/projects/-root/memory/MEMORY.md`.
2. Run `git log -20` to see what's landed since 2026-05-21.
3. Check if SP-1 has shipped — affects which provider is primary, which env vars exist, which files exist.
4. Check `.remember/recent.md` for week-over-week context.

### Always check before designing
- Existing strategy registry (`src/strategies/manifest.json`) — count of live/candidate/staging/monitoring.
- Last migration number in `src/database/migrations/`.
- systemd timer status: `systemctl list-timers --no-pager`.
- Live env vars: `grep -E "^OPENCLAW_|^ALPACA_|^FMP_" /root/openclaw/.env` (don't echo values, just keys).
- doctor preflight current pass state.

### Always update at the end
- `/root/.claude/projects/-root/memory/MEMORY.md` with new project memory file.
- `/root/openclaw/CLAUDE.md` "Recent Changes" section.
- If schema changed: regenerate any agent prompts that reference DB tables.
- If new env var: add to `.env.example` (verify it exists).
- Discord notification routing if new alert types.

### Pitfalls (from prior incidents)
- **Lifecycle silent strip**: any new top-level field on manifest entries must thread through `StrategyRecord` dataclass + `from_manifest` + `to_dict` (`feedback_lifecycle_silent_strip.md`).
- **Split-source freshness**: file vs DB stores must agree; engine reads DB by default; doctor should check both (`feedback_silent_failure_pattern.md`).
- **OPG/MOO unreliable on paper**: never use TIF=opg; use post-9:31 TIF=day (`feedback_opg_paper_unreliable.md`).
- **Liquidator audit status overloaded**: `result_status='closed'` ≠ filled; always cross-check `alpaca position list` (`feedback_liquidator_audit_status_overloaded.md`).
- **Pre-flight schema checks before dispatching subagents**: saved 3 wasted runs on D1 (`feedback_d1_schema_drift.md`).
- **Master parquets append-only**: NEVER delete rows/columns/tickers/date ranges (`feedback_never_delete_master_data.md` + `/root/openclaw/CLAUDE.md` core invariant).

### Brainstorming workflow expectation
1. Read this handoff + SP-1 spec + recent CLAUDE.md changes.
2. Invoke `superpowers:brainstorming` skill.
3. Ask one clarifying question at a time (do not batch).
4. Propose 2-3 approaches before designing.
5. Present design in sections; user approves between sections.
6. Write spec to `docs/superpowers/specs/YYYY-MM-DD-spN-<topic>-design.md`.
7. Self-review the spec (placeholders, contradictions, ambiguity, scope).
8. User reviews spec.
9. Invoke `superpowers:writing-plans` to author the implementation plan.

---

## 7. Quick-Reference Locations

```
Specs:                    /root/openclaw/docs/superpowers/specs/
Plans:                    /root/openclaw/docs/superpowers/plans/
SP-1 spec:                /root/openclaw/docs/superpowers/specs/2026-05-21-sp1-provider-cutover-design.md
This handoff:             /root/openclaw/docs/superpowers/specs/2026-05-21-data-provider-overhaul-handoff.md
Project CLAUDE.md:        /root/openclaw/CLAUDE.md
User CLAUDE.md:           /root/CLAUDE.md
Memory index:             /root/.claude/projects/-root/memory/MEMORY.md
Strategy registry:        /root/openclaw/src/strategies/manifest.json
Lifecycle code:           /root/openclaw/src/strategies/lifecycle.py
Daily orchestrator:       /root/openclaw/src/execution/pipeline_orchestrator.py
LangGraph cycle:          /root/openclaw/src/agent/graphs/daily-cycle.js
Sizer (live):             /root/openclaw/src/execution/regime_blended_sizer_live.py
Doctor:                   /root/openclaw/src/maintenance/doctor.py
System checks:            /root/openclaw/src/system_checks/
.env:                     /root/openclaw/.env (sensitive)
Alpaca CLI:               /root/go/bin/alpaca
Operator dashboard:       :7870 (fundjohn-dashboard.service)
User dashboard:           :80 / :3000 (johnbot.service embedded)
MasterMind chat:          :7871 (mastermind-chat.service)
FinBERT:                  :7872 (finbert-sentiment.service)
```
