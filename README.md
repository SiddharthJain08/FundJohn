# FundJohn / OpenClaw v2.0

**Autonomous bot-network hedge fund.** Three LLM agents plus a hardcoded data + signal pipeline, orchestrated through Discord, running on a single VPS. Runs a portfolio of 25 hardcoded strategies daily at market close with zero LLM tokens, then uses LLMs only for on-demand analysis (research synthesis, performance reports, operator requests).

> 📘 **Canonical docs** — [PIPELINE.md](PIPELINE.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [LEARNINGS.md](LEARNINGS.md)
> All three files are kept in sync with `HEAD` and are the source of truth. `SYSTEM_REPORT.md` is legacy and preserved for history only.
> **Last verified**: 2026-04-18 against HEAD `beea4cd`

---

## TL;DR

- **3 agents** — BotJohn (Opus, PM + veto), ResearchJohn (Sonnet, on-demand synthesis), TradeJohn (Sonnet, sizing).
- **1 zero-LLM daily pipeline** — 16:20 ET Mon–Fri; regime → signals → memos → Kelly opt → Alpaca paper orders. No LLM tokens consumed.
- **25 strategies** — 6 live, 15 paper, 1 staging, 1 deprecated. All pure Python; no LLM tokens at execution time.
- **6 MCP providers** — FMP, Polygon/Massive, SEC EDGAR, Tavily (tier 1) · Alpha Vantage, Yahoo/yfinance (tier 2).
- **Postgres + Redis** — lifecycle, signals, orders in Postgres; budget mode, locks, flow cache in Redis.
- **Discord-native** — every stage posts to a dedicated channel; operators interact via `@FundJohn` mentions.
- **Dollar budget** — $400/month default, with daily guardrails at $20 (YELLOW) / $35 (RED). Overridable via `config/budget.json`.

---

## Architecture (one-screen)

```
                       ┌──────────────────┐
                       │     OPERATOR     │  (Discord + web dashboard)
                       └────────┬─────────┘
                                │  @FundJohn mentions / approve / veto
                                ▼
                  ┌─────────────────────────────┐
                  │  BotJohn 🦞  (opus-4-6)     │
                  │  Orchestrator · PM · Veto   │  iter 40 · $1.00/call
                  └──────┬──────────┬───────────┘
                         │ spawns   │ spawns (on-demand)
                         ▼          ▼
        ┌─────────────────┐   ┌──────────────────┐
        │  ResearchJohn   │   │  TradeJohn       │
        │  (sonnet-4-6)   │   │  (sonnet-4-6)    │
        │  memos → report │   │  report → sized  │
        │  iter 15·$0.30  │   │  signals         │
        └─────────────────┘   └──────────────────┘

        ┌──────────────────────────────────────────────────────────┐
        │  Daily Pipeline — hardcoded, zero LLM                    │
        │  16:20 ET: regime HMM → signals cache → signal runner    │
        │  → post_memos.py → trade_agent.py (Kelly+Alpaca) → SSE  │
        │  src/ingestion/* · src/execution/* · 6 MCPs              │
        └──────────────────────────────────────────────────────────┘
```

Full architecture: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Daily cycle (ET)

The 16:20 ET pipeline is **entirely zero-LLM** — hardcoded Python from start to finish. LLM agents (ResearchJohn, TradeJohn) are on-demand only and are not triggered automatically.

| Time | What happens | Component | Tokens |
|---|---|---|---|
| Continuous | Stock OHLCV via yfinance bulk download | `src/pipeline/collector.js` + `master_dataset.py` | 0 |
| Continuous | Options EOD via Massive S3 flat files | `src/ingestion/massive_client.py` | 0 |
| Continuous | Live options flow via Massive WebSocket `OA.*` | `massive-ws.service` → `massive_ws.py::MassiveOptionsCapture` | 0 |
| Continuous | Fundamentals, SEC filings, news | `src/ingestion/pipeline.py` · `edgar_client.py` | 0 |
| **16:20 Mon–Fri** | **HMM regime classifier** → writes `workspaces/default/regime.json` | `scripts/run_market_state.py` | **0** |
| **16:20 (chained)** | **Build signals cache** from master dataset | `workspaces/default/tools/signals_cache.py --build` | **0** |
| **16:20 (chained)** | **Run all 25 strategies** → persist `Signal` rows to Postgres | `workspaces/default/tools/signal_runner.py` | **0** |
| **16:20 (chained)** | **Write per-strategy memos** → Discord `#strategy-memos` | `src/execution/post_memos.py` | **0** |
| **16:20 (chained)** | **Kelly optimization** across T1/T2/T3 exits + bracket orders to Alpaca paper | `src/execution/trade_agent.py` | **0** |
| **16:20 (chained)** | Check report triggers (≥30 unreported completed trades → queue REPORT) | `cron-schedule.js::checkReportTriggers()` | **0** |
| **16:20 (chained)** | Broadcast `market_update` SSE → dashboard auto-refreshes | `POST /api/events/data-updated` | **0** |
| **23:59** | Redis token budget counters reset | `cron-schedule.js::resetTokenBudgets()` | 0 |
| **Sun 08:00** | Weekly memory synthesis — BotJohn consolidates learnings + reaper pass + universe sync | `swarm.init({type:'strategist', mode:'REPORT'})` | LLM |

**Authoritative source**: `src/engine/cron-schedule.js::runMarketClosePipeline()` (lines 51–128).

Full pipeline reference: **[PIPELINE.md](PIPELINE.md)**.

---

## Agents

### BotJohn (`claude-opus-4-6`)

The primary operator-facing agent and portfolio manager. Responds to `@FundJohn` mentions. Two modes:

- **flash** — single-call (<10 s) replies for status, veto, quick queries
- **PTC (Plan-Then-Commit)** — spawns subagents via `swarm.init()` for multi-step tasks (reports, approvals, deep-dives)

Config: `src/agent/config/subagent-types.json` — iter cap 40, budget $1.00/call, all 6 MCPs allowed.

### ResearchJohn (`claude-sonnet-4-6`)

On-demand research synthesis agent. Reads memos from `output/memos/*_{date}.md` and produces a consolidated research report covering: executive summary, regime assessment, per-strategy performance table, convergence analysis, warnings, and recommendation. Output goes to `output/reports/{date}_research.md` and Discord `#research-feed`.

**Not part of the automatic daily cron.** Triggered event-driven: Discord `#strategy-memos` post → `bot.js::runResearchPipeline()` → `src/execution/research_report.py`.

Config: iter cap 15, budget $0.30/call, no MCP tool access (reads files only).

### TradeJohn (`claude-sonnet-4-6`)

On-demand signal sizing agent. Reads a research report and trades Kelly-sized signals. Complements the zero-LLM `trade_agent.py` with qualitative overlay when operator requests it.

**Not part of the automatic daily cron.** Triggered event-driven: Discord `#research-feed` post → `bot.js::runTradePipeline()`.

Config: iter cap 15, budget $0.30/call, no MCP tool access (reads files only).

---

## Active strategies

### Live (6)

| ID | Class | File | Thesis |
|---|---|---|---|
| `S5_max_pain` | `MaxPainGravity` | `s5_max_pain.py` | Options-derived price attractor — underlying gravitates toward max-pain strike at expiry |
| `S9_dual_momentum` | `DualMomentum` | `s09_dual_momentum.py` | Cross-asset absolute + relative momentum (Antonacci 2012) |
| `S10_quality_value` | `QualityValue` | `s10_quality_value.py` | Quality-value factor composite: ROE × gross margin × low debt × low P/B |
| `S12_insider` | `InsiderClusterBuy` | `s12_insider.py` | SEC Form 4 cluster buys — ≥ 3 insiders buy within 30 days, dollar-weighted |
| `S15_iv_rv_arb` | `IVRVArb` | `s15_iv_rv_arb.py` | IV / RV spread arbitrage — sell vol when IV > 1.25× RV |
| `S_custom_jt_momentum_12mo` | `JTMomentum12Mo` | `S_custom_jt_momentum_12mo.py` | Jegadeesh-Titman 12-month momentum with 1-month skip |

### Paper — v2 / hybrid (3)

| ID | Class | Thesis |
|---|---|---|
| `S23_regime_momentum` | `RegimeMomentumStrategy` | Regime-conditioned momentum — long only in LOW_VOL, exit in CRISIS |
| `S24_52wk_high_proximity` | `FiftyTwoWeekHighProximityStrategy` | 52-week-high breakout — buy within 2% of high, stop below high |
| `S25_dual_momentum_v2` | `DualMomentum` | S9 successor candidate with extended lookback periods |

### Paper — HV (volatility-first, 13 strategies)

All are pure vol/options strategies derived from peer-reviewed literature. No S_HV18.

| ID | Class | Thesis / citation |
|---|---|---|
| `S_HV7_iv_crush_fade` | `IVCrushFade` | Sell vol after peak IV rank (Stein 1989) |
| `S_HV8_gamma_theta_carry` | `GammaThetaCarry` | BUY_VOL when gamma/|theta| ≥ 1.5 ATM |
| `S_HV9_rv_momentum_div` | `RVMomentumDivergence` | Price/RV divergence (Bollerslev 2009) |
| `S_HV11_cross_stock_dispersion` | `CrossStockDispersion` | Decorrelated high-IV (Drechsler 2011) |
| `S_HV12_vrp_normalization` | `VRPNormalization` | VRP z-score mean-reversion (Carr & Wu 2009) |
| `S_HV13_call_put_iv_spread` | `CallPutIVSpread` | ATM call − put IV (Cremers & Weinbaum 2010) |
| `S_HV14_otm_skew_factor` | `OTMSkewFactor` | OTM put skew vs ATM call IV (Xing, Zhang & Zhao 2010) |
| `S_HV15_iv_term_structure` | `IVTermStructure` | IV term-structure backwardation/contango |
| `S_HV16_gex_regime` | `GEXRegime` | Dealer gamma exposure regime (Bollen & Whaley 2004) |
| `S_HV17_earnings_straddle_fade` | `EarningsStraddleFade` | SELL_VOL when implied > 1.20× realised near earnings (Muravyev & Pearson 2020) |
| `S_HV19_iv_surface_tilt` | `IVSurfaceTilt` | Vega·OI-weighted surface centroid tilt (Carr & Wu 2009) |
| `S_HV20_iv_dispersion_reversion` | `IVDispersionReversion` | Cross-section IV rank z-score mean-reversion (Driessen 2009, Goyal & Saretto 2009) |

### Staging / Deprecated

| ID | State | Notes |
|---|---|---|
| `S_HV10_triple_gate_fear` | staging | Blocked: needs `unusual_flow` from Massive WS Redis cache — fires when `massive-ws.service` is active |
| `S_custom_momentum_trend_v1` | deprecated | Orphaned; flagged by Audit R3 (2026-04-13); no DB registry row |

Lifecycle source of truth: `src/strategies/manifest.json` + Postgres `strategy_registry` (dual-write via `LifecycleStateMachine.transition()`).

**Promotion gate** (paper → live): **Sharpe ≥ 0.5** AND **max drawdown ≤ 20%**. Enforced in `src/strategies/lifecycle.py::LifecycleStateMachine`.

States: `candidate → paper → live → monitoring → deprecated → archived`.

---

## Kelly sizing math

`src/execution/trade_agent.py` runs Kelly optimization against every signal after the zero-LLM stage. Constants:

| Constant | Value | Meaning |
|---|---|---|
| `MAX_POSITION_PCT` | `0.05` | Hard cap per signal (5% of equity) |
| `MIN_KELLY` | `0.005` | Minimum net Kelly to be called GREEN (actionable) |
| `HALF_KELLY` | `0.50` | Safety fraction applied to raw Kelly |
| `CAPTURE_RATIO` | `0.80` | Slippage haircut — only 80% of target reward is modelled as captured |

Algorithm for each signal:
1. `p = p_hit_upper(entry, stop, target, mu_daily, sigma_daily)` — GBM two-barrier probability target is hit before stop.
2. For each exit (T1, T2, T3): `R = (target - entry) / (entry - stop)` (reward-to-risk ratio).
3. `kelly_raw = (p·R - (1-p)) / R`
4. `kelly_net = kelly_raw × HALF_KELLY`
5. `kelly_pos = clip(kelly_net, 0, MAX_POSITION_PCT)`
6. Best pair per signal = max `kelly_net` across T1/T2/T3.
7. GREEN if `kelly_net > MIN_KELLY`.

Position size stored as a fraction (e.g. `0.014` = 1.4% of equity). Dashboard multiplies by 100 for display.

Regime position scaling is applied at signal generation time by `BaseStrategy.regime_position_scale()`:

| Regime | Scale |
|---|---|
| `LOW_VOL` | 1.00 |
| `TRANSITIONING` | 0.55 |
| `HIGH_VOL` | 0.35 |
| `CRISIS` | 0.15 |

---

## Standing Orders (behavioural contract)

`AGENTS.md` defines six standing orders every agent obeys:

| # | Rule | Enforced in |
|---|---|---|
| SO-1 | Budget gate — no LLM call when `budget:mode == RED` | `pipeline_orchestrator.py`, `swarm.init()` |
| SO-2 | Lifecycle gate — only `live` / `paper` strategies reach `trade_agent`; sizing is **half-Kelly × regime-scale**, capped at `MAX_POSITION_PCT` (5% of equity) | `trade_agent.py`, `registry.py::get_approved_strategies` |
| SO-3 | Research gate — LLM trade layer runs only after research report exists | `pipeline_orchestrator.py` step ordering |
| SO-4 | Negative EV auto-veto — BotJohn skips without prompting if EV < 0 | `trade_agent.py`, BotJohn prompt |
| SO-5 | Max-DD escalation — strategy DD > 20% auto-demotes live → monitoring | `lifecycle.py` + report triggers |
| SO-6 | Memo format — canonical schema (lifecycle, regime, signal, targets, params) | `post_memos.py` |

Full rules: `AGENTS.md`. Rationale: [LEARNINGS.md §2](LEARNINGS.md).

---

## Data sources & MCP providers

| Provider | Tier | Fallback | Used for |
|---|---|---|---|
| **FMP** | 1 | Yahoo | Financials, ratios, peers, earnings calendar, price targets, universe sync |
| **Polygon / Massive** | 1 | Alpha Vantage | **Options only** — chain (IV surface, Greeks, OI), S3 flat files (`us_options_opra`), live `OA.*` WebSocket |
| **SEC EDGAR** | 1 | — | 10-K / 10-Q / 8-K / Form 4 (structured + full text) |
| **Tavily** | 1 | — | News search, press releases, earnings call transcripts |
| Alpha Vantage | 2 | — | Macro data (GDP, CPI, rates), technical indicators, intraday |
| Yahoo / yfinance | 2 | — | **All stock OHLCV** (yfinance bulk download), VIX, short interest |

> **Critical**: Polygon and Massive are the **same service** with the **same API key** (`MASSIVE_SECRET_KEY` = `POLYGON_API_KEY` value). The plan is "options starter" — authorises options S3 flat files and options WebSocket (`OA.*`) **only**. Stock OHLCV comes exclusively from yfinance. Never route stock price requests to Polygon/Massive — they 403 on this plan.

Config: `src/agent/config/servers.json`. Rate-limited Python modules auto-generated to `workspaces/default/tools/*.py` at startup. Shared rate-limiter: `tools/_rate_limiter.py` (per-provider token buckets).

### Live options flow (Massive WebSocket)

`src/ingestion/massive_ws.py::MassiveOptionsCapture` runs as `massive-ws.service`:
1. At startup: loads `data/master/options_eod.parquet` → builds `_prev_oi` (contract → previous-close OI).
2. On each `OA.*` event: parses OCC symbol (`O:AAPL240117C00150000` → underlying, expiry, type, strike); accumulates session volume into `_session_vol[und][expiry][strike][type]`.
3. After each update: computes `unusual_call_flow = session_call_vol > 0.30 × prev_call_oi`; writes to Redis key `massive:flow:{underlying}` (JSON, 4-hour TTL).

`fetch_polygon_flow()` in `src/ingestion/pipeline.py` checks this Redis key first (uses it if < 2 hours old) before falling through to REST. `S_HV10_triple_gate_fear` is unblocked when this cache is populated.

---

## Database schema

Postgres 16 in Docker. 27 migrations at `src/database/migrations/*.sql`.

### Key tables

| Table | Migration | Purpose |
|---|---|---|
| `strategy_registry` | 001 | Strategy lifecycle rows — ID, status, promotion history |
| `universe_config` | 001 | Ticker universe membership over time |
| `price_data` | 001 | Daily OHLCV per ticker |
| `pipeline_runs` | 002 | Pipeline run audit log |
| `token_usage` | 003 | Per-workspace / per-agent / per-day token and dollar spend |
| `execution_signals` | 012 | Signals emitted per strategy per date: direction, entry, stop, targets, position_size_pct, status |
| `signal_pnl` | 012 | Daily P&L rows for each open signal: close_price, unrealized_pnl_pct, days_held |
| `signal_performance` | 012 | Closed-trade outcomes: realized_pnl_pct, close_reason, reported flag |
| `orders` | 012 | Alpaca bracket order records |
| `insider_transactions` | 017 | SEC Form 4 structured rows (consumed by S12_insider) |
| `data_ledger` | materialized view | Data-coverage audit (refreshed weekly by cron) |

### Key Redis keys

| Key | TTL | Value |
|---|---|---|
| `budget:mode` | 1 h | `GREEN` / `YELLOW` / `RED` |
| `budget:daily_usd` | 1 h | Today's dollar spend |
| `budget:monthly_usd` | 1 h | 30-day trailing spend |
| `pipeline:running:{YYYY-MM-DD}` | intraday | Soft lock (prevents double-trigger) |
| `pipeline:resume_checkpoint` | — | `{run_date, next_step}` JSON — resume on crash/RED |
| `queue:report:{workspace}` | — | List of queued REPORT invocations |
| `massive_ws:pipeline_fired_today` | 6 h | Set by `MassiveEODCapture` if it fires the pipeline — prevents cron duplicate |
| `massive:flow:{underlying}` | 4 h | Unusual options flow JSON from `MassiveOptionsCapture` |
| `token_usage:{workspace}:{date}` | 1 d | Per-day token counters, cleared by 23:59 cron |

---

## Discord surface

| Channel | Posted by | Content |
|---|---|---|
| `#strategy-memos` | DataBot | Per-strategy memos (lifecycle state, regime, direction, targets, params) |
| `#research-feed` | ResearchDesk | Consolidated research report + curator run summaries |
| `#trade-signals` | TradeDesk | GREEN signals with Kelly sizing + EV + Alpaca bracket order receipts |
| `#trade-reports` | TradeDesk | Alpaca paper execution log (fills, errors, performance) |
| `#server-map` | BotJohn | Auto-updated 4-message command reference (refreshed on startup and via `/refresh-map`) |
| `#ops` | BotJohn + dashboard heartbeat | Pipeline state, budget mode, alerts |

### Commands

```
@FundJohn status                  # pipeline + budget + regime snapshot
@FundJohn approve <signal_id>     # route to live broker (operator only; paper bypasses this)
@FundJohn veto <signal_id>        # reject signal with reason
@FundJohn report <strategy_id>    # enqueue REPORT invocation for a strategy

# Opus Corpus Curator (Phase 1–5; Saturday 10:00 ET timer)
!john /curator run                      # full curation pass + promote high + spot-check
!john /curator dry-run                  # rate existing corpus, no persistence
!john /curator status                   # last 5 runs (cost, duration, buckets)
!john /curator sample [N]               # last N decisions with reasoning
!john /curator calibration              # bucket pass-rates + false positives/negatives
!john /curator re-curate <failure_mode> # re-rate papers blocked on that data gap
!john /curator promote                  # promote latest run's high-bucket only

# Funnel analytics
!john /hit-rate [30d]                   # ingested → curator_high → hunter → ready → backtest → promoted
!john /data-demand                      # missing data features × papers blocked × provider suggestions
!john /data-roi                         # expected paper unlocks per $1k of monthly data spend
```

---

## Web dashboard

`src/channels/api/server.js` serves a dark-theme monitoring dashboard on `DASHBOARD_PORT` (default 3000).

### Pages

**Market** (default) — scrollable ticker sidebar, sector overview cards, per-ticker OHLCV chart with range + type selectors, news feed per ticker.

**Portfolio** — real Alpaca paper account data + Postgres signal data:

| Section | API endpoint | Source |
|---|---|---|
| Alpaca account row | `GET /api/portfolio/account` | Alpaca paper `/v2/account` — equity, cash, buying power, day P&L, day P&L% |
| Strategy stats row | `GET /api/portfolio/summary` | `execution_signals` + `signal_pnl` — open count, closed count, win rate, avg realized P&L |
| P&L curve (toggle) | `GET /api/portfolio/pnl-curve?days=90` | `signal_pnl` — 90d avg unrealized P&L% |
| Value $ curve (toggle) | `GET /api/portfolio/value-curve?period=1M` | Alpaca `/v2/account/portfolio/history` — historical portfolio equity in USD |
| Active Positions | `GET /api/portfolio/positions` | `execution_signals` LEFT JOIN latest `signal_pnl` row, status = 'open' |
| Closed Trades | `GET /api/portfolio/history` | `signal_pnl` JOIN `execution_signals` WHERE status = 'closed' LIMIT 100 |

### SSE auto-refresh

`GET /events` — server-sent event stream. Events:
- `{"type":"pipeline"}` — updates pipeline state badge
- `{"type":"market_update"}` — calls `loadMarket()` + `refreshPipeline()` (re-fetches all market data without page reload)

`POST /api/events/data-updated` — called by `runMarketClosePipeline()` at pipeline end → broadcasts `market_update` to all connected clients.

Manual **↺** refresh button in header triggers `loadMarket()` + `refreshPipeline()` immediately.

### Scroll architecture

`#portfolio-page` uses `position:absolute;inset:0;overflow-y:auto` within the positioned `#view-wrap` container. Inner content lives in `#pf-inner` (flex column, unconstrained height). This separates the scroll boundary from the flex layout — required to make scroll reliable across all browsers. See [LEARNINGS.md §16](LEARNINGS.md).

---

## Budget governance

Managed by `src/budget/enforcer.js` + `config/budget.json` + Redis. Budget is dollar-based, not token-count-based.

| Mode | Trigger (any) | Effect |
|---|---|---|
| GREEN | default | All phases run |
| YELLOW | daily ≥ $20 or monthly ≥ 75% of $400 | Skip news phase; reduce fundamentals to weekly (Sundays only) |
| RED | daily ≥ $35 or monthly ≥ 90% of $400 | Price collection only; all PTC ops require manual trigger |

`swarm.init()` reads `budget:mode` before spawning any subagent. `pipeline_orchestrator.py` reads it before `post_memos`. When budget recovers from RED, `checkPipelineResume()` re-spawns the orchestrator with `--force-resume` at the checkpoint.

---

## Environment variables (`.env`)

```bash
# LLM
ANTHROPIC_API_KEY=...

# Data providers (Tier 1)
FMP_API_KEY=...
POLYGON_API_KEY=...         # = MASSIVE_SECRET_KEY — same service, same key
TAVILY_API_KEY=...
SEC_USER_AGENT=...          # e.g. "YourName contact@example.com"

# Data providers (Tier 2)
ALPHA_VANTAGE_API_KEY=...

# Massive WebSocket (options flow)
MASSIVE_SECRET_KEY=...                              # same value as POLYGON_API_KEY
MASSIVE_WS_REALTIME_BASE=wss://socket.massive.com
MASSIVE_WS_DELAYED_BASE=wss://delayed.massive.com

# Infrastructure
POSTGRES_URI=postgresql://user:pass@localhost:5432/fundjohn
REDIS_URL=redis://localhost:6379
DISCORD_BOT_TOKEN=...
WORKSPACE_ID=cad1a456-0b65-40ae-8be6-3530e36c53c2

# Broker (Alpaca paper)
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Dashboard
DASHBOARD_PORT=3000
```

---

## Directory layout

```
/root/openclaw/
├── README.md · PIPELINE.md · ARCHITECTURE.md · LEARNINGS.md
├── AGENTS.md · CLAUDE.md · IDENTITY.md · MEMORY.md · USER.md
├── SYSTEM_REPORT.md              (legacy — preserved for history)
├── agents/                       agent identity + prompt .md (botjohn, researchjohn, tradejohn)
├── config/                       budget.json, runtime config
├── core/                         signal_pipeline.py · gates/ (correlation_gate.py)
├── docker-compose.yml            postgres:16 + redis:7
├── johnbot.service               systemd unit (Discord bot + Node orchestrator)
├── scripts/                      run_market_state.py · orchestrator.js (legacy) · pipeline-runner.js
├── src/
│   ├── agent/
│   │   ├── config/               models.js · servers.json · subagent-types.json
│   │   ├── graph/                workflow.js state machine
│   │   ├── middleware/           9-layer stack + deployment-gate + token-budget
│   │   ├── prompts/              system + subagent .md prompt files
│   │   ├── subagents/            swarm.js · lifecycle.js · types.js
│   │   └── tools/                MCP tool generators + registry
│   ├── budget/                   enforcer.js
│   ├── channels/
│   │   ├── api/server.js         web dashboard (market + portfolio pages, SSE, Alpaca proxy)
│   │   └── discord/              bot.js · agent-personas.js · setup.js · notifications.js
│   ├── database/
│   │   ├── migrations/           27 SQL migrations (001_initial.sql … 027_*.sql)
│   │   └── redis.js              Redis client
│   ├── engine/                   cron-schedule.js (all cron jobs)
│   ├── execution/
│   │   ├── engine.py             master execution engine + confluence detection
│   │   ├── pipeline_orchestrator.py  Python supervisor with checkpointing
│   │   ├── post_memos.py         per-strategy memo writer → Discord
│   │   ├── research_report.py    consolidated research doc (on-demand)
│   │   ├── trade_agent.py        Kelly optimizer + Alpaca paper orders
│   │   ├── alpaca_trader.py      Alpaca bracket order executor
│   │   └── runner.js             runDailyClose()
│   ├── ingestion/
│   │   ├── pipeline.py           3-layer async ETL (fetch → transform → cache)
│   │   ├── massive_client.py     S3 flat-file downloader (options EOD)
│   │   ├── massive_ws.py         Massive WS client (MassiveWSClient, MassiveOptionsCapture)
│   │   ├── edgar_client.py       SEC EDGAR Form 4 / 10-K / 10-Q / 8-K
│   │   └── run_universe_sync.py  FMP universe sync → universe_config table
│   ├── pipeline/                 collector.js (Node-side yfinance coordinator)
│   ├── security/                 auth + secret redaction
│   ├── skills/                   skill packs
│   └── strategies/
│       ├── base.py               BaseStrategy ABC + Signal dataclass + regime scales
│       ├── lifecycle.py          LifecycleStateMachine
│       ├── registry.py           _IMPL_MAP + get_approved_strategies()
│       ├── manifest.json         lifecycle source-of-truth mirror
│       └── implementations/      24 strategy .py files + decommissioned/
├── tests/
└── workspaces/default/
    ├── memory/                   signal_patterns.md · trade_learnings.md · regime_context.md · fund_journal.md
    ├── output/                   memos/ · reports/ · signals/
    └── tools/                    generated MCP modules · signals_cache.py · signal_runner.py · master_dataset.py
```

Full file-by-file map: [ARCHITECTURE.md §12](ARCHITECTURE.md).

---

## Systemd services

### `johnbot.service`
Discord bot + Node orchestrator. Entry point: `src/channels/discord/bot.js`.

```ini
[Service]
ExecStart=/usr/bin/node /root/openclaw/src/channels/discord/bot.js
Environment=CLAUDE_BIN=/usr/local/bin/claude
Environment=CLAUDE_UID=1001
User=root
Restart=on-failure
```

### `openclaw-curator.service` + `.timer`
Saturday 10:00 America/New_York sweep — broad arXiv fetch, OpenAlex (SSRN + NBER + top-5 finance journals + 11-author watchlist), then Opus 4.7 corpus curation, then promotion of the high-bucket to `research_candidates`. Unit files at `docs/curator.{service,timer}`; installed to `/etc/systemd/system/` and enabled.

```ini
[Timer]
OnCalendar=Sat *-*-* 10:00:00 America/New_York
Persistent=true
```

Status: `systemctl list-timers openclaw-curator.timer`. Ad-hoc trigger: `systemctl start openclaw-curator.service`. Cost per weekly sweep: ~$10–20 (Opus 4.7 corpus curator + Haiku PaperHunter on promoted subset).

### `/etc/systemd/system/massive-ws.service`
Massive WebSocket options flow capture. Runs independently; never pauses for budget mode.

```ini
[Unit]
Description=Massive WebSocket — live options flow + EOD capture
After=network-online.target johnbot.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/openclaw
EnvironmentFile=/root/openclaw/.env
ExecStart=/usr/bin/python3 src/ingestion/massive_ws.py all
Restart=always
RestartSec=10
```

Both services are enabled at boot. Check status: `systemctl status johnbot.service` / `systemctl status massive-ws.service`.

The bot runs as root, but `claude-bin` subprocesses run as `claudebot` (uid 1001) for sandboxing.

---

## Running it locally / on the VPS

**Requirements**: Node 20+, Python 3.11, Docker + Compose, Claude CLI (`claude-bin`), Discord bot token, MCP provider API keys.

```bash
# 1. Clone + install
git clone https://github.com/SiddharthJain08/FundJohn.git
cd FundJohn
npm install
pip install -r requirements.txt --break-system-packages

# 2. Environment
cp .env.example .env
# Fill in all keys (see Environment variables section)

# 3. Infrastructure
docker compose up -d        # postgres + redis

# 4. Run (local dev)
node src/channels/discord/bot.js

# 4-alt. Run (VPS prod)
systemctl restart johnbot.service
systemctl restart massive-ws.service
journalctl -u johnbot -f
```

Full deployment workflow, rollback, migrations: [ARCHITECTURE.md §13](ARCHITECTURE.md).

### Spot-checking endpoints

```bash
# Portfolio API
curl http://localhost:3000/api/portfolio/positions
curl http://localhost:3000/api/portfolio/summary
curl http://localhost:3000/api/portfolio/account
curl http://localhost:3000/api/portfolio/history

# Trigger manual pipeline + SSE broadcast
curl -X POST http://localhost:3000/api/events/data-updated

# Redis — options flow cache
redis-cli keys 'massive:flow:*' | head -10
redis-cli get 'massive:flow:AAPL'

# Budget mode
redis-cli get budget:mode

# Pipeline lock
redis-cli get 'pipeline:running:2026-04-18'
```

---

## Stack

- **Runtime** — Node 20 (orchestrator + Discord + dashboard), Python 3.11 (execution + strategies + ingestion).
- **LLM** — Claude Opus 4.6 (BotJohn), Sonnet 4.6 (ResearchJohn + TradeJohn), Haiku 4.5 (context compaction only).
- **Storage** — Postgres 16 (lifecycle, signals, orders, insider_transactions, token_usage), Redis 7 (budget, locks, flow cache, queues).
- **Broker** — Alpaca paper-trading API (`paper-api.alpaca.markets`). Live routing is a future gate behind `@FundJohn approve`.
- **MCPs** — FMP, Polygon/Massive (options only), SEC EDGAR, Tavily, Alpha Vantage, Yahoo/yfinance.
- **Infra** — Hostinger VPS · Ubuntu 22.04 · systemd · docker-compose · GitHub origin.

---

## What's new / changed recently

- **2026-04-20** — **Opus Corpus Curator — Phases 1 through 5 shipped end-to-end**:
  - **Phase 1 (instrumentation)** — migrations `032_research_corpus` + `033_paper_gate_decisions`; structured per-gate decisions emitted from PaperHunter, ResearchJohn, validate, auto_backtest, and lifecycle. `paper_hit_rate_funnel` view + `/hit-rate` Discord command. Historical backfill (`src/ingestion/backfill_gate_decisions.py`) synthesises `research_corpus` rows for pre-corpus candidates and stitches orphaned decision rows.
  - **Phase 2a (curator)** — `corpus-curator` subagent (Opus 4.7, 1M context, $8/call budget); prompt at `src/agent/prompts/subagents/corpus-curator.md`. Orchestrator `src/agent/curators/corpus_curator.js` batches 100 papers/call, persists per-batch, promotes confidence ≥ 0.75 to `research_candidates` with a 600/week hard cap. Runs Saturday 10:00 ET via `openclaw-curator.service`/`.timer`.
  - **Phase 2b (analytics)** — `src/ingestion/openalex_discovery.py` replaces NBER/SSRN scrapers (both 404-protected). 7 venues + 11-author watchlist + citation-graph expansion via `referenced_works`. Migration `034_missing_data_demand` + `/data-demand` command surface which missing data features block the most papers, with `data_provider_recommendations` seed table.
  - **Phase 3 (calibration loop)** — migration `035_curator_calibration` adds `curator_bucket_calibration`, `curator_false_positives`, `curator_false_negatives` views. `_loadCalibrationFeedback()` injects pass-rates + 5 rotating miss examples into the cached prompt prefix with 60d lookback. Spot-check promotion (weighted random sample of med/low papers) closes the loop so false negatives become detectable.
  - **Phase 4a–c (source breadth)** — added Journal of Finance, RFS, JFE, JFQA, Quantitative Finance as OpenAlex venues; added `AUTHOR_WATCHLIST` (Fama, French, Jegadeesh, Asness, Cremers, Pedersen, Moskowitz, Koijen, Lettau, Hirshleifer, Hou); `expand_citation_graph()` pulls one-hop references from high-bucket picks.
  - **Phase 5a–d (closed loop)** — `/curator re-curate <failure_mode>` re-rates blocked papers after a data-provider add, reports bucket transitions. Migration `036_strategy_type_priors` classifies papers into 13 categories with SQL heuristics; per-type rates injected into prompt. Migration `037_gate_predictions` — curator now emits per-gate pass probabilities (paperhunter / researchjohn / convergence); confidence = product. `curator_gate_calibration` shows over-confidence bias per gate. Migration `038_data_roi` + `/data-roi` command rank data providers by expected paper unlocks per $1k of monthly spend.
  - **Session totals** — 1,544 papers in `research_corpus`, 1,145 curator evaluations, 1,307 structured gate decisions, 3 simulated weekly cycles at $41.74 total curator cost, first false positive recorded (Price-Path Convexity @ 0.80 → failed convergence).
  - **Infra** — `johnbot.service` restarted; `#server-map` now a 4-message reference (was 3), with Curator + Analytics subsections. `run-subagent-cli.js` now pipes prompts via stdin (avoids E2BIG on batch-of-100 inputs). `research-orchestrator.js` emits structured `paper_gate_decisions` at every gate.
- **2026-04-18 (`beea4cd`)** — **Massive WebSocket options integration + dashboard portfolio page**:
  - `src/ingestion/massive_ws.py` — live options flow capture via Massive `OA.*` WebSocket; writes unusual-flow signals to Redis (`massive:flow:{underlying}`, 4-hour TTL). Runs as `massive-ws.service`.
  - `src/ingestion/massive_client.py` — S3 flat-file download for options EOD data (`us_options_opra`). All stock OHLCV removed from Massive client; yfinance handles all stock prices.
  - `src/channels/api/server.js` — full Portfolio page (Alpaca account row, strategy stats row, Active Positions + Closed Trades tables, P&L % / Value $ curve toggle). SSE `market_update` fires after each pipeline run; dashboard auto-refreshes. Dashboard startup fix: `display !== 'block'` check instead of `=== 'none'` (CSS inline style vs computed style).
  - `src/engine/cron-schedule.js` — WS-aware gate (skips cron if Massive WS already fired via `massive_ws:pipeline_fired_today` Redis key); broadcasts `market_update` SSE after close pipeline.
  - Position sizing display fixed: `execution_signals.position_size_pct` stored as fraction (0.014 = 1.4%); dashboard now multiplies by 100 before rendering.
  - `src/execution/alpaca_trader.py` — `execute_alpaca_orders` + `build_alpaca_post` fully implemented.
  - 27 DB migrations, `core/` signal pipeline with correlation + concentration gates, 7 subagent types.
- **2026-04-16 (`9f326f3`)** — **Alpaca paper trading wired into `trade_agent.py`**. Green signals (kelly_net > MIN_KELLY) auto-submit as bracket orders sized `kelly_pos × equity`. No operator approval required for paper mode.
- **2026-04-16** — `pipeline_orchestrator` wired to cron, signature fixes for S_HV13–15.
- **2026-04-15** — removed strategist cron, dynamic memo labels, S_HV17 promoted to paper.
- **2026-04-14** — **DataJohn removed**, replaced by hardcoded data pipeline (`src/ingestion/*`, `src/pipeline/collector.js`). Biggest architectural change since reinit; see [LEARNINGS.md §3](LEARNINGS.md).
- **2026-04-14** — S_HV13 through S_HV20 added (options-literature cohort). S_HV18 intentionally skipped.
- **2026-04-13** — Audit R3: canonicalised numbered strategy IDs, moved five originals to `decommissioned/`.

Full timeline: [LEARNINGS.md §13](LEARNINGS.md).

---

## License

Private. © Sid / FundJohn. Not for public distribution.
