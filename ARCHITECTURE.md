# FundJohn — System Architecture

> **System**: FundJohn / OpenClaw v2.0
> **Last verified**: 2026-04-18 against HEAD `beea4cd`
> **Companion docs**: [PIPELINE.md](PIPELINE.md) · [LEARNINGS.md](LEARNINGS.md) · [README.md](README.md)

This is the technical deep-dive. Every module, file, connection, MCP, database table, Discord channel, and cron job is listed here with its purpose and its path on the VPS (`/root/openclaw/`).

---

## 1. Stack at a glance

| Layer | Technology | Location on VPS |
|---|---|---|
| Host | Hostinger VPS, Ubuntu 22.04 | `/root/openclaw` |
| Process supervisor | systemd | `johnbot.service` |
| Orchestrator runtime | Node 20+ | `src/agent/`, `src/channels/`, `src/engine/` |
| Execution runtime | Python 3.11 | `src/execution/`, `src/strategies/`, `scripts/`, `workspaces/default/tools/` |
| LLM | Claude CLI (`claude-bin`) running as uid 1001 (`claudebot`) | spawned by `src/agent/subagents/swarm.js` |
| Persistent store | PostgreSQL 16 | docker-compose, host `:5432` |
| Cache & queues | Redis 7 | docker-compose, host `:6379` |
| Message bus | Discord (discord.js) | `src/channels/discord/bot.js` |
| Market data | 6 MCP providers | `src/agent/tools/mcp/*.js` |

Container wiring lives in `docker-compose.yml`. Migrations are mounted into the Postgres container at `/docker-entrypoint-initdb.d`.

---

## 2. Agent topology (3 agents + hardcoded DataPipeline)

v2.0 reduced the agent count from four to three. DataJohn (the LLM data collector) was removed in commit `ef8e2cf` and replaced by hardcoded collectors under `src/ingestion/` and the Python signal stage. See [LEARNINGS.md §3](LEARNINGS.md).

```
                      ┌──────────────────┐
                      │     OPERATOR     │  (Discord chat, web dashboard)
                      └────────┬─────────┘
                               │
                               ▼
                 ┌─────────────────────────────┐
                 │   BotJohn 🦞                │  claude-opus-4-6
                 │   Orchestrator / PM         │  iteration cap 40, $1.00 budget
                 │   Veto authority            │  modes: PM_TASK
                 └─────────┬─────────┬─────────┘
                           │         │
              spawns       │         │  spawns
                           ▼         ▼
         ┌──────────────────────┐   ┌──────────────────────┐
         │  ResearchJohn        │   │  TradeJohn           │
         │  claude-sonnet-4-6   │   │  claude-sonnet-4-6   │
         │  Reads memos →       │   │  Reads research →    │
         │  research report     │   │  sized signals       │
         │  iter 15, $0.30      │   │  iter 15, $0.30      │
         └──────────────────────┘   └──────────────────────┘

         ┌──────────────────────────────────────────────────┐
         │   DataPipeline  (hardcoded, no LLM)              │
         │   src/ingestion/* + 6 MCP providers              │
         │   runs continuously; meets 16:20 ET deadline     │
         └──────────────────────────────────────────────────┘
```

### 2.1 Agent configs

Authoritative configuration: **`src/agent/config/subagent-types.json`**.

| Agent | Prompt file | Tools allowlist | Iter cap | $ cap | Veto |
|---|---|---|---|---|---|
| `botjohn` | `src/agent/prompts/subagents/botjohn.md` | all 6 MCPs | 40 | $1.00 | ✅ |
| `researchjohn` | `src/agent/prompts/subagents/researchjohn.md` | none (reads files only) | 15 | $0.30 | ❌ |
| `tradejohn` | `src/agent/prompts/subagents/tradejohn.md` | none (reads files only) | 15 | $0.30 | ❌ |

Model wiring: **`src/agent/config/models.js`** — `MODELS.orchestrator = claude-opus-4-6`, `MODELS.primary = claude-sonnet-4-6`, `MODELS.fast = claude-haiku-4-5-20251001`. The `fast` model is used for session pruning and context compaction only.

Session pruning: every subagent invocation has `maxToolResultTokens: 2000` and `maxToolResultAge: 10` — keeps long-running agents' context bounded.

### 2.2 Agent identity and rules

Repo root `.md` files define behaviour:

- **`IDENTITY.md`** — BotJohn's identity doc.
- **`AGENTS.md`** — Standing Orders SO-1 through SO-6 (see §3).
- **`CLAUDE.md`** — 3-agent overview, context retention rules, VPS paths, deployment workflow.
- **`MEMORY.md`** — BotJohn's persistent memory (preferences, architecture decisions).
- **`SOUL.md`** — Operator-facing philosophy / tone.
- **`SYSTEM_REPORT.md`** — 33KB legacy system report (preserved for historical context; supersedes points are captured here).
- **`USER.md`** — Operator profile.

Per-agent identity:

- `agents/botjohn/IDENTITY.md` + `agents/botjohn/PROMPT.md`
- `agents/researchjohn/PROMPT.md`
- `agents/tradejohn/PROMPT.md`
- `agents/datajohn/*` — **DEPRECATED** (retained only for history; no runtime reference)

---

## 3. Standing Orders (behavioural contract)

`AGENTS.md` defines six standing orders every agent obeys:

| Order | Rule | Enforced in |
|---|---|---|
| SO-1 | Budget gate — no LLM call if `budget:mode == RED` | `pipeline_orchestrator.py`, `swarm.init()` |
| SO-2 | Lifecycle gate — only approved strategies reach `trade_agent.py`; paper/monitoring sized at 50% | `trade_agent.py`, `registry.py::get_approved_strategies` |
| SO-3 | Research gate — `trade_agent` runs only after `research_report` succeeds | `pipeline_orchestrator.py` step ordering |
| SO-4 | Negative EV auto-veto — if expected-value < 0, BotJohn skips without prompting | `trade_agent.py`, BotJohn prompt |
| SO-5 | Max-DD escalation — strategy with DD > 20% is auto-demoted live → monitoring | `lifecycle.py` + report triggers |
| SO-6 | Memo format — strategy memos follow the canonical schema (lifecycle, regime, signal, targets, params) | `post_memos.py` |

---

## 4. Middleware stack (9 layers)

Loaded in order by `src/agent/middleware/index.js` and wrapped around every LLM call:

| # | File | Purpose |
|---|---|---|
| 1 | `cache-control.js` | Marks long-lived prompt blocks with `cache_control: {type: "ephemeral"}` |
| 2 | `secret-redaction.js` | Redacts API keys and tokens before they enter a prompt |
| 3 | `steering.js` | Injects operator steering (e.g. "hold all new positions today") |
| 4 | `skills-loader.js` | Loads the requested skill's SKILL.md into context if referenced |
| 5 | `workspace-context.js` | Attaches the current workspace directory listing |
| 6 | `context-management.js` | Summarises older turns when context window tightens |
| 7 | `large-result-eviction.js` | Evicts >2KB tool results older than 10 turns |
| 8 | `multimodal-injection.js` | Injects chart PNGs and tables as messages rather than text |
| 9 | `hitl.js` | Human-in-the-loop gate — surfaces high-impact actions to operator |

Adjacent but not part of the 9-layer stack:

- **`deployment-gate.js`** — governs **subagent** spawning only; does not apply to BotJohn's direct responses. The top-level constant `PERMITTED_MODES = new Set(['DEPLOY', 'REPORT'])` is the default allow-list, but `validateInvocation(agentType, mode, prompt)` branches by agent type to also admit: `PM_TASK` (BotJohn himself, always allowed), `SIGNAL_PROCESSING` (compute / equity-analyst / research processing a confluence candidate), `MARKET_STATE` (scheduled data-prep), `RISK_SCAN` (emergency strategist), `STRATEGY_PERFORMANCE` + `TRADE` (report-builder). Everything else — including `TIER_A`/`TIER_B` data-prep and direct research invocation — is rejected. Prompts are additionally scanned against `BLOCKED_PATTERNS` (regex list for deprecated phrases like `run.*signal`, `fetch.*data`, `diligence`, etc.).
- **`pipeline-activity.js`** — writes heartbeat to Redis so the dashboard can show live pipeline state.
- **`token-budget.js`** — updates the `budget:mode` Redis key on each spawn.

`src/agent/subagents/swarm.js::init()` runs the deployment gate first, then applies the 9-layer stack, then spawns `claude-bin` as uid 1001 with the computed prompt. `swarm.js` also injects the current token-budget constraint into the system prompt.

---

## 5. Workflow state machine

`src/agent/graph/workflow.js` implements a four-stage graph:

```
 plan ─▶ validate ─▶ execute ─▶ report
  │                    │           │
  └── rejection ◀──────┘           │
                                   ▼
                                done
```

`runDailyClose()` in `workflow.js` delegates to `src/execution/runner.js::runDailyClose()`. Rejection loops back to `plan` and re-enters `validate` — used for SO-3 research-gate failures.

---

## 6. MCP providers (market data layer)

`src/agent/config/servers.json` lists six providers, each with tier + fallback:

| Provider | Tier | Fallback | Tool count | Typical use |
|---|---|---|---|---|
| **FMP** | 1 | yahoo | 18 | Financials, ratios, peers, earnings, price targets |
| **Polygon / Massive** | 1 | alpha_vantage | 12 | **Options only** — chain, snapshots, S3 flat files (EOD), live `OA.*` WebSocket. Same service/key. |
| **SEC EDGAR** | 1 | — | 5 | 10-K / 10-Q / 8-K / Form 4 full text + structured |
| **Tavily** | 1 | — | 2 | News search, press releases, transcripts |
| **Alpha Vantage** | 2 | — | 15 | Macro (GDP, CPI, rates), technical indicators, intraday |
| **Yahoo / yfinance** | 2 | — | 6 | **All stock OHLCV** (yfinance bulk download), VIX, short interest |

> ⚠️ **Polygon = Massive**: same service, same API key. The "options starter" plan grants options S3 + options WebSocket only. Stock data (OHLCV) is provided exclusively by yfinance (`master_dataset.py::refresh_prices_bulk`, `collector.js`). Never route stock price requests to Polygon/Massive — they will 403.

Each provider has a Node-side generator at `src/agent/tools/mcp/<name>.js`. `generatePython(server)` produces a Python module (`tools/<name>.py`) exporting rate-limited callables. All providers share `tools/_rate_limiter.py` which enforces per-provider token-bucket limits via `_acquire_token(_PROVIDER)`.

Generation entry point: `src/agent/tools/registry.js::generateToolModules(workspaceDir)`. Called at startup; writes to `workspaces/default/tools/`.

### 6.1 Environment variables (`.env.example`)

**LLM + MCPs**: `ANTHROPIC_API_KEY`, `FMP_API_KEY`, `POLYGON_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `TAVILY_API_KEY`, `SEC_USER_AGENT`.

**Infra**: `POSTGRES_URI`, `REDIS_URL`, `DISCORD_BOT_TOKEN`, `WORKSPACE_ID`.

**Broker (Alpaca paper trading)**: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL` (`https://paper-api.alpaca.markets` for paper mode).

**Massive WebSocket (options flow)**: `MASSIVE_SECRET_KEY` (same value as `POLYGON_API_KEY`), `MASSIVE_WS_REALTIME_BASE` (`wss://socket.massive.com`), `MASSIVE_WS_DELAYED_BASE` (`wss://delayed.massive.com`). Note: Massive = Polygon — same service, same key, clearer name. Options starter plan authorises options S3 and `OA.*` WebSocket only. **Stock data is not available via Massive/Polygon on this plan; use yfinance.**

---

## 7. Strategies — taxonomy

Full list at `src/strategies/manifest.json`. Grouped by origin:

### 7.1 Canonical live (6)

| ID | Class | File | Thesis |
|---|---|---|---|
| `S5_max_pain` | `MaxPainGravity` | `s5_max_pain.py` | Options-derived price attractor |
| `S9_dual_momentum` | `DualMomentum` | `s09_dual_momentum.py` | Cross-asset absolute + relative momentum |
| `S10_quality_value` | `QualityValue` | `s10_quality_value.py` | Quality-value factor composite |
| `S12_insider` | `InsiderClusterBuy` | `s12_insider.py` | SEC Form 4 cluster-buy signal |
| `S15_iv_rv_arb` | `IVRVArb` | `s15_iv_rv_arb.py` | IV / RV spread arbitrage |
| `S_custom_jt_momentum_12mo` | `JTMomentum12Mo` | `S_custom_jt_momentum_12mo.py` | Jegadeesh-Titman 12-month momentum |

### 7.2 Paper — v2 / hybrid (3)

- `S23_regime_momentum` — `RegimeMomentumStrategy` — regime-conditioned momentum.
- `S24_52wk_high_proximity` — `FiftyTwoWeekHighProximityStrategy` — 52-week-high breakout.
- `S25_dual_momentum_v2` — `DualMomentum` in `S25_dual_momentum.py` — successor candidate to S9.

### 7.3 Paper — HV (volatility-first) (13)

`S_HV7` … `S_HV20` (no S_HV18). Each is a peer-reviewed vol/options thesis:

| ID | Class | Thesis / citation |
|---|---|---|
| `S_HV7_iv_crush_fade` | `IVCrushFade` | Sell vol after peak iv_rank (Stein 1989) |
| `S_HV8_gamma_theta_carry` | `GammaThetaCarry` | BUY_VOL when gamma/|theta| ≥ 1.5 ATM |
| `S_HV9_rv_momentum_div` | `RVMomentumDivergence` | Price/RV divergence (Bollerslev 2009) |
| `S_HV10_triple_gate_fear` | `TripleGateFear` | **staging** — needs `unusual_flow` data |
| `S_HV11_cross_stock_dispersion` | `CrossStockDispersion` | Decorrelated high-IV (Drechsler 2011) |
| `S_HV12_vrp_normalization` | `VRPNormalization` | VRP z-score mean-reversion (Carr & Wu 2009) |
| `S_HV13_call_put_iv_spread` | `CallPutIVSpread` | ATM call - put IV (Cremers & Weinbaum 2010) |
| `S_HV14_otm_skew_factor` | `OTMSkewFactor` | OTM put skew vs ATM call IV (Xing, Zhang & Zhao 2010) |
| `S_HV15_iv_term_structure` | `IVTermStructure` | IV term-structure backwardation/contango |
| `S_HV16_gex_regime` | `GEXRegime` | Dealer gamma exposure regime (Bollen & Whaley 2004) |
| `S_HV17_earnings_straddle_fade` | `EarningsStraddleFade` | SELL_VOL when implied > 1.20× realised near earnings (Muravyev & Pearson 2020) |
| `S_HV19_iv_surface_tilt` | `IVSurfaceTilt` | Vega·OI-weighted surface centroid tilt (Carr & Wu 2009) |
| `S_HV20_iv_dispersion_reversion` | `IVDispersionReversion` | Cross-section iv_rank z-score mean-reversion (Driessen 2009, Goyal & Saretto 2009) |

### 7.4 Deprecated / decommissioned

- `S_custom_momentum_trend_v1` — deprecated 2026-04-13 by Audit R3 (orphan file, no DB registry row, no HOLDING_PERIOD entry).
- `decommissioned/`: `dual_momentum.py`, `quality_value.py`, `insider_cluster_buy.py`, `iv_rv_arb.py`, `max_pain.py` — predecessors of the canonical `sXX_` versions. Retained for backtest reproducibility only; not imported anywhere.

### 7.5 Execution math (engine vs trade_agent split)

Two files share the execution-side math; keep their roles clear.

**`src/execution/engine.py`** — master execution engine. Runs the strategy loop, persists raw `Signal` rows to Postgres, and detects **cross-strategy confluence**: `detect_confluence()` writes to the `confluence_signals` table when ≥ `CONFLUENCE_MIN` (default `2`) strategies agree on the same ticker and direction. `combined_size_pct` per confluence row is used downstream by reporting.

**`src/execution/trade_agent.py`** — the Kelly-sizing layer. Reads today's signal rows (including confluence metadata when relevant) and runs:

- GBM two-barrier probability `p_hit_upper(entry, stop, target, mu_daily, sigma_daily)` for each T1/T2/T3 exit.
- `kelly_raw = (p·R - (1-p)) / R`, then `kelly_net = kelly_raw × HALF_KELLY (0.50)`.
- `kelly_pos = clip(kelly_net, 0, MAX_POSITION_PCT)` where `MAX_POSITION_PCT = 0.05`.
- Keeps the best (target, Kelly) pair per signal. GREEN if `kelly_net > MIN_KELLY (0.005)`.
- Slippage haircut: multiplies reward by `CAPTURE_RATIO (0.80)`.
- Post: condensed green summary to `#trade-signals`; no-action diagnostic otherwise.
- **Alpaca** (2026-04-16): `execute_alpaca_orders(green, run_date)` submits bracket orders (market + TP + SL) sized `kelly_pos × equity`; `build_alpaca_post()` appends the order receipt to the Discord message.

Regime scaling is applied at signal-generation time in `BaseStrategy.regime_position_scale()` — *not* inside `trade_agent.py`. `trade_agent.py` treats the incoming position sizes as already regime-adjusted and applies only Kelly + MAX_POSITION_PCT on top.

### 7.6 Strategy contract (recap)

Every strategy inherits `BaseStrategy` from `src/strategies/base.py` and implements:

```python
def generate_signals(
    self,
    prices:   pd.DataFrame,   # wide: date × ticker closes
    regime:   dict,           # regime JSON
    universe: List[str],      # tickers to consider
    aux_data: dict = None,    # optional: financials, options, earnings, insider
) -> List[Signal]
```

`Signal` is a dataclass with `ticker`, `direction` (LONG / SHORT / SELL_VOL / BUY_VOL / FLAT), `entry_price`, `stop_loss`, three targets, `position_size_pct`, `confidence` (HIGH / MED / LOW), and a free-form `signal_params` dict.

Regime position scaling (applied downstream by `trade_agent.py`):

```python
REGIME_POSITION_SCALE = {
    'LOW_VOL':       1.00,
    'TRANSITIONING': 0.55,
    'HIGH_VOL':      0.35,
    'CRISIS':        0.15,
}
```

---

## 8. Database schema

Postgres 16 in docker. All tables created in order by `src/database/migrations/*.sql`:

| Migration | Tables / changes |
|---|---|
| `001_initial.sql` | Base universe, ohlcv, workspaces |
| `002_pipeline.sql` | Pipeline run tracking |
| `003_tokens.sql` | Token usage accounting per workspace+agent+day |
| `004_coverage.sql` | Data-coverage tracking |
| `005_schema_v2.sql` | v2 normalization |
| `006_trigger_config.sql` | Report-trigger config rows |
| `007_collection_cycles.sql` | Daily collection cycle audit |
| `008_market_universe.sql` | Universe membership over time |
| `009_market_news.sql` | Tavily / AV news feed store |
| `010_security.sql` | Security events, rate-limit audit |
| `011_strategist.sql` | Legacy strategist tables (pre-v2) |
| `012_execution_engine.sql` | `signals`, `signal_performance`, `orders` |
| `013_reported_flag.sql` | `signal_performance.reported` for triggers |
| `014_strategy_versions.sql` | Version rows for immutable strategies |
| `015_data_agent.sql` | Legacy DataJohn tables — retained but unused |
| `016_agent_registry.sql` | `agent_registry` — per-agent runtime state |
| `017_insider_transactions.sql` | SEC Form 4 structured rows |
| `018_technicals_trim.sql` | Trim unused technical cols |
| `019_technicals_sma20.sql` | Add SMA20 |
| `020_drop_technicals.sql` | Drop the standalone `technicals` table |
| `021_add_ratio_columns.sql` | Add valuation ratio columns |

Redis keys in active use:

- `budget:mode` — GREEN / YELLOW / RED
- `token_usage:{workspace}:{date}` — daily token counters
- `pipeline:running:{YYYY-MM-DD}` — pipeline soft-lock
- `pipeline:resume_checkpoint` — resume JSON `{run_date, next_step}`
- `queue:report:{workspace}` — queued REPORT invocations

---

## 9. Discord interface

`src/channels/discord/bot.js` is the entry point. Channel topology is defined in `src/channels/discord/setup.js` and agent-channel binding in `src/channels/discord/agent-personas.js`. Active channels include `#strategy-memos`, `#research-feed`, `#trade-signals`, `#trade-reports` (and operator channels).

Personas:

- **DataBot** — auto-posts strategy memos to `#strategy-memos` (runner.js after signal runner).
- **ResearchDesk** — listens on `#strategy-memos`; when a memo is posted, calls `runResearchPipeline()` → `research_report.py` → `#research-feed`.
- **TradeDesk** — listens on `#research-feed`; calls `runTradePipeline()` → `trade_agent.py`. Posts green-signal summaries + appended Alpaca paper-order receipts to `#trade-signals`, and longer performance / risk digests to `#trade-reports`. Channel keys bound in `agent-personas.js`: `['trade-signals', 'trade-reports']`.
- **BotJohn** — operator-facing. Responds to `@FundJohn` mentions. Two response modes: **flash** (single-call <10s reply) and **PTC** (Plan-Then-Commit — spawns subagents via `swarm.init` for multi-step tasks).

Bot command surface (Discord slash + `@`-mention):

| Command / trigger | Handler | Effect |
|---|---|---|
| `@FundJohn status` | flash | Dumps pipeline / budget / regime state |
| `@FundJohn approve <signal_id>` | PTC | BotJohn routes to broker (live path — Alpaca paper orders fire automatically without this gate) |
| `@FundJohn veto <signal_id>` | flash | Marks signal rejected, writes reason |
| `@FundJohn report <strategy_id>` | PTC | Enqueues REPORT invocation |
| `#strategy-memos` post | event | Starts research pipeline |
| `#research-feed` post | event | Starts trade pipeline |

---

## 9b. Web dashboard (`src/channels/api/server.js`)

Express app, served on `DASHBOARD_PORT` (default 3000), dark-theme UI built inline as a template string in `getDashboardHtml()`.

### Pages

**Market** (default) — scrollable ticker sidebar, sector overview cards, OHLCV chart with range + type toggles, news feed per ticker.

**Portfolio** — opened via nav button, accessed at runtime from Postgres + Alpaca:

| Section | Source | Notes |
|---|---|---|
| Alpaca account row | `GET /api/portfolio/account` → Alpaca paper `/v2/account` | Equity, cash, buying power, day P&L, day P&L%, invested |
| Strategy stats row | `GET /api/portfolio/summary` | Open count, closed count, win rate, avg/best/worst realized P&L |
| P&L curve chart | `GET /api/portfolio/pnl-curve?days=90` | 90d avg unrealized P&L% from `signal_pnl` table |
| Value $ curve toggle | `GET /api/portfolio/value-curve?period=1M` → Alpaca `/v2/account/portfolio/history` | Historical portfolio equity in USD |
| Active Positions | `GET /api/portfolio/positions` | `execution_signals` LEFT JOIN latest `signal_pnl` row, status='open' |
| Closed Trades | `GET /api/portfolio/history` | `signal_pnl` JOIN `execution_signals` WHERE status='closed' LIMIT 100 |

**Scroll architecture**: `#portfolio-page` is `position:absolute;inset:0;overflow-y:auto` within `#view-wrap` (the positioned flex child below header+strip). Inner content lives in `#pf-inner` (flex column, unconstrained height). This separates the scroll boundary from flex layout, guaranteeing scroll works across all browsers.

**Position sizing**: stored in `execution_signals.position_size_pct` as a fraction (e.g. `0.014` = 1.4%). Dashboard multiplies by 100 before display.

### SSE auto-refresh

`GET /events` — server-sent event stream. Client listens for:
- `{"type":"pipeline"}` — shows pipeline status in badge
- `{"type":"market_update"}` — calls `loadMarket()` + `refreshPipeline()` (re-fetches all market data without full page reload)

`POST /api/events/data-updated` — called by `runMarketClosePipeline()` in `cron-schedule.js` after the pipeline finishes; broadcasts `market_update` to all connected clients.

---

## 10. Systemd services

**`johnbot.service`** (Discord bot + Node orchestrator):

```ini
[Service]
ExecStart=/usr/bin/node /root/openclaw/src/channels/discord/bot.js
Environment=CLAUDE_BIN=/usr/local/bin/claude
Environment=CLAUDE_UID=1001
User=root
Restart=on-failure
```

**`/etc/systemd/system/massive-ws.service`** (Massive WebSocket — live options flow):

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

Both services are enabled and start on boot. `massive-ws.service` connects to `wss://socket.massive.com/options`, subscribes `OA.*`, and streams options aggregate events to the `MassiveOptionsCapture` handler. On budget YELLOW/RED it still runs (options flow is zero-token; no reason to pause it). To check status: `systemctl status massive-ws.service` / `journalctl -u massive-ws -f`.

The bot runs as root, but `claude-bin` subprocesses run as `claudebot` (uid 1001) for sandboxing. MCP tool modules are generated into `workspaces/default/tools/` at startup.

---

## 11. Scripts (ops + legacy)

`scripts/`:

- `orchestrator.js` — **legacy** diligence orchestrator (pre-v2). Spawns 5 sub-agents in parallel per ticker, assembles 12-section memo, emits `BOTJOHN_PROGRESS:` and `BOTJOHN_VERDICT:` markers. Kept for ad-hoc manual deep-dives.
- `pipeline-runner.js` — phase-based runner (diligence concurrency 3, scenario 2, trade sequential) with verdict caching.
- `run_market_state.py` — HMM regime classifier, called from cron.
- Various one-off migration / backfill scripts.

---

## 12. Workspace layout

```
/root/openclaw/
├── agents/               # agent identity/prompt .md files (botjohn, researchjohn, tradejohn)
├── config/               # runtime config (not agent config)
├── docker-compose.yml    # postgres + redis
├── johnbot/              # legacy top-level entry (reinit artifacts)
├── johnbot.service       # systemd unit
├── mcp-servers/          # MCP provider scaffolding
├── package.json          # Node deps
├── scripts/              # ops + legacy scripts
├── sp500_initial_fill.js # one-shot universe loader
├── src/
│   ├── agent/            # orchestration layer (Node)
│   │   ├── config/       # models.js, servers.json, subagent-types.json
│   │   ├── graph/        # workflow.js state machine
│   │   ├── middleware/   # 9-layer stack + deployment-gate + token-budget
│   │   ├── prompts/      # system + subagent prompts
│   │   ├── subagents/    # swarm.js, lifecycle.js, types.js
│   │   └── tools/        # MCP tool generators + registry
│   ├── backtesting/      # backtest harness
│   ├── budget/           # token accounting
│   ├── channels/
│   │   ├── api/server.js   # web dashboard (market + portfolio pages, SSE, Alpaca API proxy)
│   │   └── discord/        # bot.js, agent-personas.js, notifications.js
│   ├── database/         # migrations (27) + redis client + tokens.js
│   ├── engine/           # cron-schedule.js
│   ├── execution/        # engine.py, pipeline_orchestrator.py, post_memos.py, research_report.py
│   │                     # trade_agent.py, alpaca_trader.py, runner.js, send_report.py
│   │                     # execute_recommendation.py, handoff.py, portfolio_report.py
│   ├── ingestion/        # pipeline.py, massive_client.py, massive_ws.py, edgar_client.py, run_universe_sync.py
│   ├── pipeline/         # collector.js (yfinance bulk + options coordinator)
│   ├── security/         # auth + redaction
│   ├── skills/           # skill packs
│   ├── strategies/       # base.py, lifecycle.py, registry.py, manifest.json, implementations/
│   └── workspace/        # workspace scaffolding
├── tests/
└── workspaces/default/   # runtime workspace
    ├── memory/           # signal_patterns.md, trade_learnings.md, regime_context.md, fund_journal.md
    ├── output/           # memos/, reports/, signals/
    └── tools/            # generated MCP tool modules + signals_cache.py + signal_runner.py
```

---

## 13. Deployment workflow (for operators)

1. **Develop locally** in the repo at `fundjohn_repo`.
2. **Commit + push** to `origin/main`.
3. **On VPS**: `cd /root/openclaw && git pull && npm install && pip install -r requirements.txt --break-system-packages`.
4. **Migrations** (if new SQL): `docker compose down && docker compose up -d` — migrations auto-apply on fresh Postgres init; for in-place migrations, run the new `.sql` files manually against the live DB.
5. **Restart the bot**: `systemctl restart johnbot.service`.
6. **Watch**: `journalctl -u johnbot -f` for live logs; Discord `#ops` for pipeline status.

Rollback: `git checkout <prev-sha>` and restart. Postgres migrations are not reversed automatically — any destructive migration (e.g. `020_drop_technicals.sql`) requires a manual restore from the nightly backup before rollback.

---

## 14. Performance & cost envelope

Authoritative budget config: `config/budget.json` (fallback defaults baked into `src/budget/enforcer.js::loadConfig()`).

| Metric | Threshold / target |
|---|---|
| Monthly budget | **$400** |
| Daily burn → YELLOW | ≥ $20 |
| Daily burn → RED | ≥ $35 |
| Monthly % → YELLOW | ≥ 75% of monthly budget |
| Monthly % → RED | ≥ 90% of monthly budget |
| Signal stage latency (16:20 cron) | < 5 min target |
| Research report latency | < 3 min target |
| Trade agent latency | < 2 min target |
| MCP rate-limit hit ratio | < 1% of calls |

Spend accounting is queried from Postgres (`src/database/tokens.js::getTotalSpend(30)`); the mode is then cached in Redis `budget:mode` / `budget:daily_usd` / `budget:monthly_usd` with a 1-hour TTL for fast `@FundJohn status` reads.

---

## 15. Observability

- **Discord** — live pipeline state (`#ops`), memos (`#strategy-memos`), research (`#research-feed`), trades (`#trade-desk`).
- **Dashboard** — `src/channels/api/server.js` on port 3000 — market overview + portfolio page with live Alpaca data; SSE auto-refreshes on pipeline completion.
- **Logs** — `journalctl -u johnbot.service` for bot + spawned subagents.
- **Postgres** — `signal_performance`, `orders`, `token_usage`, `agent_registry` — richest structured signal.
- **`/root/.learnings/LEARNINGS.md`** — every agent appends its learnings here with an area tag (e.g. `memory-synthesis`, `risk-scan`).

---

*This is the technical spec. If you need "how does it flow on a given day", read [PIPELINE.md](PIPELINE.md). If you want "why was it built this way", read [LEARNINGS.md](LEARNINGS.md).*
