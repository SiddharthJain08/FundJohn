# FundJohn / OpenClaw v2.0

**Autonomous bot-network hedge fund.** Three LLM agents plus a hardcoded data + signal pipeline, orchestrated through Discord, running on a single VPS. Runs a portfolio of ~20 hardcoded strategies daily at market close with zero LLM tokens, then uses LLMs only for judgement (research synthesis, trade selection, veto).

> 📘 **Canonical docs** — [PIPELINE.md](PIPELINE.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [LEARNINGS.md](LEARNINGS.md)
> All three files are kept in sync with `HEAD` and are the source of truth. `SYSTEM_REPORT.md` is legacy and preserved for history only.
> **Last verified**: 2026-04-18 against HEAD `beea4cd`

---

## TL;DR

- **3 agents** — BotJohn (Opus, PM + veto), ResearchJohn (Sonnet, research synthesis), TradeJohn (Sonnet, sizing).
- **1 zero-LLM pipeline** — 16:20 ET daily, runs 20+ hardcoded strategies in pure Python.
- **6 MCP providers** — FMP, Polygon, SEC EDGAR, Tavily (tier 1) · Alpha Vantage, Yahoo (tier 2).
- **Postgres + Redis** — lifecycle + signals in Postgres; budgets, locks, queues in Redis.
- **Discord-native** — every stage posts to a dedicated channel; operator approves trades with `@FundJohn approve`.
- **Dollar budget** — $400/month default, with daily burn guardrails at $20 (YELLOW) / $35 (RED). Overridable via `config/budget.json`.

---

## Architecture (one-screen)

```
                       ┌──────────────────┐
                       │     OPERATOR     │  (Discord)
                       └────────┬─────────┘
                                │
                                ▼
                  ┌─────────────────────────────┐
                  │  BotJohn 🦞  (opus-4-6)     │
                  │  Orchestrator · PM · Veto   │
                  └──────┬──────────┬───────────┘
                         │          │
                         ▼          ▼
        ┌─────────────────┐   ┌──────────────────┐
        │  ResearchJohn   │   │  TradeJohn       │
        │  (sonnet-4-6)   │   │  (sonnet-4-6)    │
        │  memos → report │   │  report → signals│
        └─────────────────┘   └──────────────────┘

        ┌──────────────────────────────────────────────┐
        │  DataPipeline — hardcoded, zero LLM          │
        │  src/ingestion/*  +  6 MCPs                  │
        │  16:20 ET signal runner · regime HMM · cache │
        └──────────────────────────────────────────────┘
```

Full architecture: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Daily cycle (ET)

| Time | What happens | Tokens |
|---|---|---|
| continuous | Hardcoded data collection (`src/ingestion/*` + MCPs) | 0 |
| **16:20** | Regime + signals cache + signal runner (`src/engine/cron-schedule.js::runMarketClosePipeline`) | 0 |
| 16:20+ | `runner.js::runDailyClose()` posts strategy memos to `#strategy-memos` | 0 |
| ~16:30 | ResearchJohn reads memos, posts report to `#research-feed` | LLM |
| ~16:45 | TradeJohn reads report, posts Kelly-sized signals to `#trade-signals` + execution log to `#trade-reports`; green signals auto-submit to Alpaca paper as bracket orders | LLM |
| ~16:50 | BotJohn reviews, operator approves live routes with `@FundJohn approve <id>` (Alpaca paper bypasses approval) | LLM |
| 23:59 | Redis token counters reset | 0 |
| Sun 08:00 | Weekly memory synthesis (BotJohn consolidates learnings) | LLM |

Full pipeline reference: **[PIPELINE.md](PIPELINE.md)**.

---

## Active strategies

| ID | State | Class | Thesis |
|---|---|---|---|
| `S5_max_pain` | live | `MaxPainGravity` | Options-derived price attractor |
| `S9_dual_momentum` | live | `DualMomentum` | Cross-asset dual-momentum |
| `S10_quality_value` | live | `QualityValue` | Quality-value factor composite |
| `S12_insider` | live | `InsiderClusterBuy` | SEC Form 4 cluster buys |
| `S15_iv_rv_arb` | live | `IVRVArb` | IV / RV spread arbitrage |
| `S_custom_jt_momentum_12mo` | live | `JTMomentum12Mo` | Jegadeesh-Titman 12-mo momentum |
| `S23_regime_momentum` | paper | `RegimeMomentumStrategy` | Regime-conditioned momentum |
| `S24_52wk_high_proximity` | paper | `FiftyTwoWeekHighProximityStrategy` | 52-week-high breakout |
| `S25_dual_momentum_v2` | paper | `DualMomentum` | S9 successor candidate |
| `S_HV7` … `S_HV20` *(13 total, no S_HV18)* | paper | vol/options research cohort | IV crush, gamma-theta carry, VRP, skew, term structure, GEX, earnings straddle, dispersion, surface tilt |
| `S_HV10_triple_gate_fear` | staging | `TripleGateFear` | Blocked on `unusual_flow` data |
| `S_custom_momentum_trend_v1` | deprecated | — | Orphaned; flagged by Audit R3 |

Lifecycle source of truth: `src/strategies/manifest.json` + Postgres `strategy_registry` (dual-write via `LifecycleStateMachine.transition()`).

Promotion gate (paper → live): **Sharpe ≥ 0.5** AND **max drawdown ≤ 20%**. Enforced in `src/strategies/lifecycle.py`.

---

## Standing Orders (behavioural contract)

| # | Rule |
|---|---|
| SO-1 | Budget gate — no LLM call when `budget:mode == RED` |
| SO-2 | Lifecycle gate — only `live` / `paper` strategies reach `trade_agent`; sizing is **half-Kelly × regime-scale**, capped at `MAX_POSITION_PCT` (5% of equity). Paper vs live is not currently a sizing lever. |
| SO-3 | Research gate — `trade_agent` runs only after `research_report` succeeds |
| SO-4 | Negative EV auto-veto — BotJohn skips without prompting if EV < 0 |
| SO-5 | Max-DD escalation — DD > 20% auto-demotes live → monitoring |
| SO-6 | Memo format — canonical schema (lifecycle, regime, signal, targets, params) |

Full rules: `AGENTS.md`. Rationale: [LEARNINGS.md §2](LEARNINGS.md).

---

## Data sources & MCP providers

| Provider | Tier | Fallback | Used for |
|---|---|---|---|
| **FMP** | 1 | Yahoo | Financials, ratios, peers, earnings, price targets |
| **Polygon / Massive** | 1 | Alpha Vantage | **Options only** — options chain, S3 flat files (us_options_opra), live OA.* WebSocket |
| **SEC EDGAR** | 1 | — | 10-K / 10-Q / 8-K / Form 4 |
| **Tavily** | 1 | — | News search, press releases, transcripts |
| Alpha Vantage | 2 | — | Macro data, technical indicators |
| Yahoo Finance | 2 | — | **Stock OHLCV** (via yfinance bulk download), VIX, short interest |

> **Important**: Polygon and Massive are the same service / same API key (`MASSIVE_SECRET_KEY` = `POLYGON_API_KEY` value). The plan is "options starter" — authorises options S3 flat files and options WebSocket (`OA.*`). **Stock data comes exclusively from yfinance.** Do not use Polygon/Massive for stock prices.

Config: `src/agent/config/servers.json`. Generated Python rate-limited modules: `workspaces/default/tools/*.py`.

### Live options flow (Massive WebSocket)

`src/ingestion/massive_ws.py` runs continuously as `massive-ws.service`, subscribing to `OA.*` on `wss://socket.massive.com/options`. It accumulates per-contract session volume, compares to previous-day OI (loaded from `data/master/options_eod.parquet` at startup), and writes unusual-flow signals to Redis (`massive:flow:{underlying}`, 4-hour TTL). `fetch_polygon_flow()` in `pipeline.py` reads this cache before falling through to the REST API.

---

## Directory layout

```
fundjohn_repo/
├── README.md · PIPELINE.md · ARCHITECTURE.md · LEARNINGS.md
├── AGENTS.md · CLAUDE.md · IDENTITY.md · MEMORY.md · USER.md
├── SYSTEM_REPORT.md              (legacy — preserved for history)
├── agents/                       agent identity + prompt .md (botjohn, researchjohn, tradejohn)
├── core/                         signal_pipeline.py · gates/ (correlation_gate.py)
├── docker-compose.yml            postgres:16 + redis:7
├── johnbot.service               systemd unit (Discord bot + Node orchestrator)
├── /etc/systemd/system/massive-ws.service   systemd unit (Massive WS options flow)
├── scripts/                      ops + legacy (orchestrator.js, run_market_state.py)
├── src/
│   ├── agent/                    Node orchestration (config, middleware, subagents, prompts, plugins)
│   ├── channels/
│   │   ├── api/server.js         web dashboard (market overview + portfolio page, SSE)
│   │   └── discord/              bot.js · event routing · agent-personas
│   ├── database/migrations/      27 SQL migrations
│   ├── engine/                   cron-schedule.js
│   ├── execution/                pipeline_orchestrator.py · post_memos.py · research_report.py
│   │                             trade_agent.py · alpaca_trader.py · engine.py · runner.js
│   ├── ingestion/                pipeline.py · massive_client.py · massive_ws.py
│   │                             edgar_client.py · run_universe_sync.py
│   ├── pipeline/                 collector.js (Node-side collection coordinator)
│   └── strategies/               base.py · lifecycle.py · registry.py · manifest.json · implementations/
└── workspaces/default/           runtime workspace (memory/ · output/ · tools/)
```

Full file-by-file map: [ARCHITECTURE.md §12](ARCHITECTURE.md).

---

## Discord surface

| Channel | Posted by | Role |
|---|---|---|
| `#strategy-memos` | DataBot | Per-strategy memos from signal runner |
| `#research-feed` | ResearchJohn | Consolidated research report |
| `#trade-signals` | TradeJohn | Green signals with Kelly sizing + EV + Alpaca bracket order receipts |
| `#trade-reports` | TradeJohn | Alpaca paper-trading execution log (fills, errors, performance) |
| `#ops` | BotJohn + dashboard heartbeat | Pipeline state, budget, alerts |

### Web dashboard

`src/channels/api/server.js` serves a dark-theme monitoring dashboard on `DASHBOARD_PORT` (default 3000):

- **Market page** — live price strip, sidebar ticker list, per-ticker OHLCV chart with range selectors, sector overview cards
- **Portfolio page** — Alpaca account value row (equity / cash / day P&L / invested), strategy stats row (open positions / win rate / avg P&L), 90-day P&L curve with "P&L %" / "Value $" toggle, Active Positions table (71 rows, position sizing % displayed correctly), Closed Trades table
- **SSE** — `/events` stream pushes `pipeline` and `market_update` events; dashboard auto-reloads market data on `market_update` (fired by cron after `runMarketClosePipeline` completes)
- **Refresh button** — manual reload without page navigation

Commands:

```
@FundJohn status                  # pipeline + budget + regime snapshot
@FundJohn approve <signal_id>     # route to broker (operator only)
@FundJohn veto <signal_id>        # reject with reason
@FundJohn report <strategy_id>    # enqueue REPORT invocation
```

---

## Running it locally / on the VPS

**Requirements**: Node 20+, Python 3.11, Docker + Compose, Claude CLI (`claude-bin`), Discord bot token, MCP provider API keys.

```bash
# 1. Clone + install
git clone https://github.com/SiddharthJain08/FundJohn.git
cd FundJohn
npm install
pip install -r requirements.txt --break-system-packages

# 2. Environment (copy + fill)
cp .env.example .env

# 3. Infra
docker compose up -d     # postgres + redis

# 4. Run (local dev)
node src/channels/discord/bot.js

# 4-alt. Run (VPS prod)
systemctl restart johnbot.service
journalctl -u johnbot -f
```

Full deployment workflow, rollback, migrations: [ARCHITECTURE.md §13](ARCHITECTURE.md).

---

## Stack

- **Runtime** — Node 20 (orchestrator + Discord), Python 3.11 (execution + strategies).
- **LLM** — Claude Opus 4.6 (BotJohn), Sonnet 4.6 (Research/Trade), Haiku 4.5 (compaction).
- **Storage** — Postgres 16 (lifecycle, signals, orders, insider_transactions), Redis 7 (budget, locks, queues).
- **MCPs** — FMP, Polygon, SEC EDGAR, Tavily, Alpha Vantage, Yahoo.
- **Infra** — Hostinger VPS · systemd · docker-compose · GitHub origin.

---

## What's new / changed recently

- **2026-04-18 (`beea4cd`)** — **Massive WebSocket options integration + dashboard portfolio page**:
  - `src/ingestion/massive_ws.py` — live options flow capture via Massive `OA.*` WebSocket; writes unusual-flow signals to Redis. Runs as `massive-ws.service`.
  - `src/ingestion/massive_client.py` — S3 flat-file download for options EOD data (us_options_opra). Stock data removed; yfinance handles all stock OHLCV.
  - `src/channels/api/server.js` — full Portfolio page (Alpaca account, positions, closed trades, P&L curve with toggle). SSE `market_update` event fires after each pipeline run; dashboard auto-refreshes.
  - `src/engine/cron-schedule.js` — WS-aware gate (skips cron if Massive WS already fired); broadcasts `market_update` after close pipeline.
  - Position sizing `%` fixed: stored as fractions (0.014 = 1.4%), correctly displayed as percentages.
  - `src/execution/alpaca_trader.py` — `execute_alpaca_orders` + `build_alpaca_post` fully implemented (resolves the open question from `9f326f3`).
  - 27 DB migrations, `core/` signal pipeline with correlation + concentration gates, 7 subagent types.
- **2026-04-16 (`9f326f3`)** — **Alpaca paper trading wired into TradeJohn**. Green signals auto-submit as bracket orders sized `kelly_pos × equity`.
- **2026-04-16** — `pipeline_orchestrator` wired to cron, signature fixes for S_HV13–15.
- **2026-04-15** — removed strategist cron, dynamic memo labels, S_HV17 promoted to paper.
- **2026-04-14** — **DataJohn removed**, replaced by hardcoded data pipeline (biggest architectural change since reinit; see [LEARNINGS.md §3](LEARNINGS.md)).
- **2026-04-14** — S_HV13 through S_HV20 added (options-literature cohort).
- **2026-04-13** — Audit R3: canonicalised numbered strategy IDs, moved five originals to `decommissioned/`.

Full timeline: [LEARNINGS.md §13](LEARNINGS.md).

---

## License

Private. © Sid / FundJohn. Not for public distribution.
