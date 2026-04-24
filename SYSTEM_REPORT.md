# OpenClaw System Report
**Generated:** 2026-04-10  
**Status:** Active  
**Runtime:** `/root/openclaw/` — `johnbot.service` (systemd, user scope)

---

## 1. System Overview

OpenClaw is a zero-token hedge fund signal engine operated by a single human (the operator). The system is architected around a strict separation between:

- **Zero-token execution** — hardcoded Python strategies that run every market day on a cron schedule, producing signals with no LLM involvement
- **Token-gated LLM activation** — agents only wake up for two tasks: writing new strategy code (DEPLOY) and generating performance reports (REPORT)

The LLM layer (Claude Code CLI, `claude-bin`) is the most expensive resource. The system is designed so that it consumes approximately 0 tokens per day during normal operation. Tokens are spent exclusively on novel strategy discovery (off-hours research) and periodic reporting.

All market data, signals, confluence scores, and P&L tracking are stored in a PostgreSQL database. Redis is used for rate limiting, token bucket management, and inter-agent coordination.

---

## 2. Infrastructure

| Component | Detail |
|-----------|--------|
| **Runtime** | Claude Code CLI (`/usr/local/bin/claude-bin`), user `claudebot` (uid=1001) |
| **Discord bot** | `src/channels/discord/bot.js` — systemd service `johnbot.service` |
| **Database** | PostgreSQL 16 (docker-compose), 39 tables, ~96 MB total |
| **Cache** | Redis (docker-compose) — rate limits, token budgets, pipeline state |
| **Master data** | `data/master/` — Parquet files (prices, financials, options_eod, macro, insider) |
| **Workspace** | `/root/openclaw/workspaces/default/` — persistent strategy files and tools |
| **Working directory** | `/root/openclaw/` |
| **Python tools** | `workspaces/default/tools/*.py` — auto-loaded at startup |

---

## 3. Data Layer

### 3.1 Master Dataset

The master dataset (`data/master/`) is the single source of truth. All data flows into it; all strategies read from it. Nothing is deleted — data is append-only (deduped on date+ticker).

**Current state (PostgreSQL price_data table):**
- 215,067 price rows across 166 tickers
- Date range: 2016-04-10 → 2026-04-08
- 800 fundamental rows (100 tickers)
- 85,977 options rows (124 tickers)

**Master data files (Parquet, read by strategies):**
- `prices.parquet` — OHLCV, all tickers, up to 10 years
- `financials.parquet` — FCF yield, EV/Revenue, margins, per ticker per quarter
- `options_eod.parquet` — end-of-day options chain snapshots (IV, HV, Greeks)
- `macro.parquet` — GDP, CPI, rates, credit spreads
- `insider.parquet` — Form 4 transactions

### 3.2 Universe

**Configured universe:** SP500 (expanded from SP100 on 2026-04-10)

| Count | Description |
|-------|-------------|
| 456 | Total active tickers in `universe_config` |
| 384 | Tagged SP500 constituents |
| 389 | With fundamentals collection enabled |
| 397 | With options collection enabled |

The universe is defined in two places:
- **DB**: `universe_config` table — authoritative source used by the collector at runtime
- **Code fallback**: `src/pipeline/universe.js` → `SP500[]` — used if DB is empty
- **Python fallback**: `workspaces/default/tools/master_dataset.py` → `get_active_universe()` reads DB first, falls back to hardcoded `SP500[]`

**Index membership** is tracked per ticker (`index_membership TEXT[]`) — allows filtering to SP100 subset, benchmarks, or full SP500.

### 3.3 Data Collection

**Schedule:** Daily at **17:00 ET** (after market close). Collection runs in phases, rate-limited per provider.

| Phase | Data | Provider | Rate Limit |
|-------|------|----------|------------|
| 1 | Real-time snapshots (market hours, every 5min) | Massive (Polygon) | `polygon_req_per_min=5` |
| 2a | Historical OHLCV — full SP500 | Massive (Polygon) | Configurable |
| 2b | Technical indicators (RSI-14, SMA-50, SMA-200) | Massive (Polygon) | Shared bucket |
| 3 | Options chain EOD snapshots | Massive (Polygon) | Shared bucket |
| 4 | Fundamentals — income, ratios, FCF | FMP `/stable/` | `fmp_req_per_day=250`, `fmp_req_per_sec=2` |
| 5 | Macro data (GDP, CPI, rates) | Alpha Vantage | 25 req/day |
| 6 | Insider transactions (Form 4) | SEC EDGAR | 10 req/sec |

**Rate limiting:** Shared Redis token bucket per provider across all processes. `tools/_rate_limiter.py` wraps every API call.

**Fallback chain:**
- Prices: Massive → Alpha Vantage → Yahoo Finance
- Fundamentals: FMP → Yahoo Finance

**Configuration** (live, in `pipeline_config` table):
```
daily_trigger_time = 17:00  (ET)
polygon_req_per_min = 5
fmp_req_per_day = 250
history_days = 3650 (10 years lookback)
collect_prices, collect_options, collect_fundamentals, collect_technicals = true
```

### 3.4 Market State

The HMM (Hidden Markov Model) regime classifier runs at **16:15 ET daily** (before data collection) and produces a regime file at `.agents/market-state/latest.json`.

**Current regime (2026-04-09):**
```json
{
  "state": "HIGH_VOL",
  "confidence": 1.0,
  "stress_score": 47,
  "roro_score": 46.8,
  "position_scale": 0.35,
  "days_in_current_state": 5,
  "active_strategies": ["S1","S2","S3","S4","S5","S6","S8","S9","S10",
                         "S11","S12","S13","S15_BUY","S16","S17","S18","S19","S20"]
}
```

**Four regime states:**

| State | VIX Range | Position Scale | Description |
|-------|-----------|----------------|-------------|
| `LOW_VOL` | <18 | 100% | Bull market, full sizing |
| `TRANSITIONING` | 18-25 | 60% | Regime shift, reduced sizing |
| `HIGH_VOL` | 25-40 | 35% | Elevated stress, defensive |
| `CRISIS` | >40 | 0% (longs blocked) | Market dislocation |

**HMM features** used for state classification:
- VIX level + 5-day change
- VIX term structure slope (VIX3M/VIX)
- SPX 20-day realized volatility
- HY/IG credit spread proxy
- SPX 5-day return
- Put/call ratio 5-day MA

The model refits weekly on Mondays. State file is written to disk and Postgres (`market_regime` table). All strategies read this file — it is the regime conditioning input.

---

## 4. Signal Engine (Zero-Token Core)

The signal engine is the heart of the system. It runs every market day at 16:15 ET, consuming **0 LLM tokens**.

### 4.1 Execution Pipeline

```
16:15 ET daily (Mon-Fri)
  ↓
run_market_state.py          → regime file + HMM update
  ↓
signals_cache.py --build     → load master Parquets into memory cache
  ↓
signal_runner.py             → execute all active strategies → write to DB
  ↓
confluence scoring           → identify high-agreement candidates
  ↓
checkReportTriggers()        → queue REPORT if strategy has ≥30 unreported trades
```

### 4.2 Signal Runner (`workspaces/default/tools/signal_runner.py`)

- Loads all strategies from `workspaces/default/strategies/` via `__init__.py`
- Filters to strategies active in current regime (`strategy.is_active(regime_state)`)
- Passes each strategy the signals cache dict (no API calls during execution)
- Collects `SignalResult` objects from each strategy
- Scores confluence: tickers where ≥2 strategies agree
- Writes results to `execution_signals` table and `signal_pnl` table
- Writes `work/signal_runner/latest.json` with daily summary

### 4.3 Confluence Scoring

A ticker becomes a **high-conviction candidate** when:
- `MIN_CONFLUENCE = 2` — at least 2 active strategies agree on direction
- `confidence >= 0.50` — minimum signal confidence threshold

High-confluence candidates are queued for optional LLM review (SIGNAL_PROCESSING mode), though this path is rarely activated.

---

## 5. Strategy Library

### 5.1 Deployed Strategies

Three strategies are currently deployed and registered as v1. All files are immutable (chmod 444).

#### MV01 — Momentum-Value Composite (`mv01_momentum_value.py`)
**Tier:** 3 (Equity)  
**Regime active:** LOW_VOL, TRANSITIONING, HIGH_VOL (scaled), CRISIS (off)  
**Universe:** SP500 equities only (ETFs excluded)  
**Required data:** `prices`, `financials`

The simplest valid intersection of two persistent equity anomalies (Jegadeesh & Titman 1993 momentum + FCF yield/EV-Rev value). Ranks all universe names on a composite score (50% momentum rank + 50% value rank). Names in the top quintile (≥80th percentile) generate LONG signals; bottom quintile (≤20th) generate SHORT.

Key mechanics:
- **Momentum signal:** 12-1 month return (12-month return minus most recent 1 month, to avoid reversal)
- **Value signal:** FCF yield minus EV/Revenue (higher FCF yield + lower EV/Rev = higher rank)
- **Absolute momentum filter:** If SPY 12-month return is negative (bear market), all LONG signals are suppressed
- **Regime scaling:** Position scale applied by TradeJohn based on regime state

Parameters (v1):
```python
LONG_THRESHOLD  = 0.80   # composite rank >= 80th percentile → LONG
SHORT_THRESHOLD = 0.20   # composite rank <= 20th percentile → SHORT
MIN_HISTORY     = 252    # minimum 1 year of price history required
```

---

#### CA02 — Cross-Asset Correlated Movements (`ca02_cross_asset.py`)
**Tier:** 2 (Cross-Asset)  
**Regime active:** All regimes  
**Universe:** SP500 names mapped by sector  
**Required data:** `prices` (must include cross-asset tickers: TLT, HYG, LQD, UUP, GLD, CPER)

Cross-asset relationships create predictable sector-level rotations. This strategy computes 5-day z-scores for five macro indicators over a 20-day rolling window, maps each indicator to the sectors it historically leads, then aggregates to individual ticker signals via sector membership.

Five cross-asset indicators:
1. **Bond yields (TLT):** Rising yields → VALUE rotation (XLF, XLE) over GROWTH (XLK, XLC)
2. **Credit stress (HYG/LQD ratio):** Spread widening → risk-off (XLV, XLP, XLU lead)
3. **Dollar strength (UUP):** Strong dollar → domestic/defensive over commodity exporters
4. **Gold breakout:** Gold rally → defensive sectors (XLV, XLP, XLU, XLRE)
5. **Copper trend (CPER):** Copper rally → cyclicals (XLE, XLB, XLI, XLY)

Parameters (v1):
```python
LOOKBACK_Z = 20    # z-score window in days
SIGNAL_Z   = 1.5   # z-score threshold for a "strong" signal
MIN_AGREE  = 2     # minimum indicators that must agree on sector direction
```

---

#### BS03 — Black-Scholes Options Mispricing (`bs03_options_mispricing.py`)
**Tier:** 4 (Options)  
**Regime active:** All regimes (BUY_VOL in HIGH_VOL/CRISIS, SELL_VOL in LOW_VOL)  
**Universe:** SP500 names with options data  
**Required data:** `options_eod`, `prices`

When implied volatility (IV) is significantly below recent realized volatility (HV20), options are underpriced relative to their BSM theoretical value. The strategy computes BSM fair values using HV20 as the volatility input, compares them to market prices, and signals a long volatility position when the market is underpricing risk.

Key mechanics:
- Filters to ATM options: within 5% of spot price, 14-45 DTE
- Computes BSM call/put value using HV20 (20-day historical realized vol)
- If `market_price < BSM_value × (1 - MIN_MISPRICING_PCT)` → BUY_VOL signal
- Contains pure Python BSM implementation (scipy optional, numerical approx fallback)
- Risk-free rate hardcoded at 5% (TODO: update from macro dataset)

Parameters (v1):
```python
MIN_MISPRICING_PCT = 0.15   # option must be ≥15% below BSM value
MIN_IV_HV_DISCOUNT = 0.10   # IV must be ≥10% below HV20
MIN_DTE            = 14     # minimum days to expiry
MAX_DTE            = 45     # maximum days to expiry  
MAX_MONEYNESS      = 0.05   # within 5% of spot (ATM filter)
```

---

### 5.2 Strategy Versioning

Strategies are **immutable after deployment**. The version manager (`src/engine/strategy-version-manager.js`) enforces this:

- Each strategy file is `chmod 444` (read-only) after first deployment
- Parameter adjustments create a **new versioned file** (`mv01_momentum_value_v2.py`)
- The original file is never modified or deleted
- Only one version of each strategy is active at a time
- All versions are tracked in the `strategy_versions` table with full audit history

**Version registry (current state):**
```
MV01_momentum_value_v1     active  v1  deployed 2026-04-10  signals: 0
CA02_cross_asset_v1        active  v1  deployed 2026-04-10  signals: 0
BS03_options_mispricing_v1 active  v1  deployed 2026-04-10  signals: 0
```

**Discord commands:**
- `/adjust-strategy {base_id} PARAM=value reason: {why}` — creates new versioned file
- `/strategy-versions {base_id}` — shows full version history

### 5.3 Strategy Architecture (BaseStrategy)

All strategies inherit from `workspaces/default/strategies/base.py`:

```python
class BaseStrategy:
    STRATEGY_ID  = ''       # unique identifier
    NAME         = ''
    TIER         = 0        # 1-5 taxonomy tier
    SIGNAL_TYPE  = ''       # EQUITY, MACRO, OPTIONS, STAT_ARB
    REQUIRES     = []       # dataset keys needed from cache
    REGIME_SCALE = {}       # position scale multiplier per regime state

    def generate_signals(self, universe, cache, regime, preferences) -> list[SignalResult]
    def is_active(self, regime_state) -> bool
    def safe_generate(...) -> list[SignalResult]   # wrapped with error handling
```

`SignalResult` schema:
```python
SignalResult(
    strategy_id,    # strategy that fired
    ticker,         # equity ticker
    signal,         # +1 (long), -1 (short), 0 (flat)
    signal_type,    # EQUITY / OPTIONS / MACRO
    confidence,     # 0.0–1.0
    key_metrics,    # dict of supporting numbers
    notes           # one-line human description
)
```

### 5.4 Planned Strategy Taxonomy (20 strategies, 5 tiers)

Currently deployed: 3 of 20. The strategist agent discovers and deploys the remaining 17 during off-hours research sessions.

| Tier | Range | Category | Description |
|------|-------|----------|-------------|
| 1 | S1–S5 | Macro/Regime | Always computed: HMM state, RORO, SPX trend, vol regime, credit spreads |
| 2 | S6–S8 | Cross-Asset | RORO composite, crypto lead-lag, commodity regimes. CA02 covers S7-area. |
| 3 | S9–S14 | Equity | Momentum (S9), Quality-Value (S10), Earnings revisions (S11), Insider cluster (S12), Drift post-earnings (S13), 52-week high (S14) |
| 4 | S15–S18 | Options | IV/RV arb (S15), BSM mispricing (BS03/S16), Dispersion (S17), Put/call contrarian (S18) |
| 5 | S19–S20 | Stat Arb | Pairs trading, ETF NAV arbitrage |

**Also in registry (not versioned, status `approved`):**
- `S9_dual_momentum` — Dual Momentum (12-1 month)
- `S10_quality_value` — Quality-Value screen
- `S12_insider` — Insider Cluster Buy
- `S15_iv_rv_arb` — IV/RV Arbitrage

---

## 6. Agent Layer

Agents are Claude Code CLI subprocesses spawned by the swarm (`src/agent/subagents/swarm.js`). Each runs in an isolated session with its own prompt context. The **deployment gate** (`src/agent/middleware/deployment-gate.js`) enforces which agents can activate and in which modes.

### 6.1 Permitted Activation Modes

| Mode | Who activates | When |
|------|--------------|------|
| `DEPLOY` | Strategist | Off-hours (6pm–6am ET weekdays, all weekend), budget ≥20%, pipeline idle |
| `REPORT` | Report-builder | After ≥30 unreported closed trades for a strategy |
| `SIGNAL_PROCESSING` | Research, Compute, Equity-analyst | When a high-confluence signal is queued for optional LLM review |
| `MARKET_STATE` | Data-prep | Daily market state preprocessing |
| `RISK_SCAN` | Strategist | Emergency scan, bypasses off-hours constraint |
| `PM_TASK` | BotJohn | Any operator Discord message — always permitted |

**Blocked forever:**
- Per-ticker diligence (removed system — superseded by signal engine)
- Research invoked directly (only via SIGNAL_PROCESSING pipeline)

---

### 6.2 Agent Descriptions

---

#### BotJohn (Primary PM Agent)
**File:** `src/agent/main.js` (PTC mode), `src/agent/flash.js` (Flash mode)  
**Prompt:** `src/agent/prompts/base.md` + all component files  
**Discord:** All channels

The master orchestrator. Every Discord message from the operator routes through BotJohn. He decides whether to answer directly (Flash mode, <10s), spawn subagents (PTC mode), or route to the zero-token pipeline.

**Responsibilities:**
- Interpret operator intent and route to the correct handler
- Spawn and supervise subagents via `swarm.js`
- Enforce all standing orders (SO-1 through SO-6)
- Post signals, reports, and system status to Discord
- Block any invocation that falls outside DEPLOY or REPORT scope

**Flash mode commands** (instant, no subagent):
`/ping`, `/status`, `/quote TICKER`, `/profile TICKER`, `/calendar TICKER`, `/market`, `/rate`, `/verdict TICKER`, `/help`

**PTC mode triggers:**
Any complex task requiring subagent spawning — strategy research, performance reports, SIGNAL_PROCESSING pipeline.

---

#### Strategist
**File:** `src/agent/prompts/subagents/strategist.md`  
**Mode:** DEPLOY (off-hours only)  
**Scheduler:** `src/agent/graph/strategist-scheduler.js`

The strategy discovery engine. Runs exclusively during off-hours (6pm–6am ET weekdays, all weekend) when token budget ≥20% and pipeline is idle. Automatically pauses if another agent activates or market hours begin.

**Session lifecycle:**
```
EXPLORE → BACKTEST → VALIDATE → write .py file → register → deployment report → DONE
```

1. **EXPLORE:** Research market anomaly hypotheses using master dataset and literature. Generates `strategy_hypotheses` table entries.
2. **BACKTEST:** Implements hypothesis as a Python `generate_signals()` function. Runs `tools/backtest.py` with walk-forward validation. Minimum bar: Sharpe ≥0.8, max DD ≤15%, statistical significance ≥95%.
3. **VALIDATE:** Runs `tools/deployment_validator.py` — checks class structure, zero-API-call requirement, memory limits, and signal sanity.
4. **WRITE:** Saves `.py` file to `workspaces/default/strategies/`. Registers in `strategy_registry` as `status='pending_approval'`. Writes deployment report to `results/strategies/`.

The strategy is **inactive by default** — operator must run `/approve-strategy {id}` in Discord before it enters the live signal pipeline.

**Yield gate:** Checks token budget and pipeline activity before every major step. A checkpoint file is saved so sessions can resume if interrupted.

---

> **Historical note (2026-04-23):** Earlier iterations of this doc described
> five placeholder subagents — `data-prep`, `research`, `compute`,
> `equity-analyst`, `report-builder` — as part of the per-ticker diligence
> flow. Those agents never had prompt files in `src/agent/prompts/subagents/`
> and were never part of the live signal pipeline. Their responsibilities
> moved into Python (`run_market_state.py`, `signal_runner.py`,
> `strategy_runner.py`, `trade_handoff_builder.py`) and into TradeJohn
> (risk veto + sizing). The placeholder types have been removed from
> `subagent-types.json`, verification contracts, batch eligibility, and
> the deployment gate. Live subagents are listed in §1.
---

## 7. Middleware Stack

Every agent invocation passes through 9 middleware layers in order:

```
message → [cache-control] → [secret-redaction] → [steering] → [skills-loader]
       → [workspace-context] → [context-management] → [large-result-eviction]
       → [multimodal-injection] → [hitl] → agent
```

| Middleware | Purpose |
|-----------|---------|
| `cache-control` | Check verdict cache before spawning subagents (avoid redundant runs) |
| `secret-redaction` | Strip API keys and credentials from context before LLM sees it |
| `steering` | Redis-backed steering rules (operator can inject runtime instructions) |
| `skills-loader` | Load skill definitions for the 3 active skills (portfolio, screen, watchlist) |
| `workspace-context` | Inject workspace paths, agent.md, preferences.json into context |
| `context-management` | Token budget enforcement — truncate or reject if budget exceeded |
| `large-result-eviction` | Remove large tool results from context when approaching limits |
| `multimodal-injection` | Handle image/chart attachments from operator |
| `hitl` | Human-in-the-loop gate — surfaces decisions requiring operator approval |

---

## 8. Cron Schedule

All scheduled operations run via `src/engine/cron-schedule.js` (loaded in `bot.js`).

| Time (ET) | Schedule | Task | Tokens |
|-----------|----------|------|--------|
| 16:15 | Mon-Fri | Market-state → signals cache → signal runner → confluence → report queue check | 0 |
| 17:00 | Daily | Data collection (prices, options, fundamentals) via `collector.js` | 0 |
| 18:00–06:00 | Every 30min | Strategist eligibility check → DEPLOY session if conditions met | Variable |
| Weekends | Every 30min | Strategist always eligible | Variable |
| 23:59 | Daily | Token budget counters reset in Redis | 0 |

**Note:** The 16:15 signal pipeline runs on the previous day's data (collected at 17:00 the prior day). Data collected at today's 17:00 feeds tomorrow's 16:15 signal run. This is the intended flow: collect close data → run signals next day → generate trade candidates.

---

## 9. Active Skills

Skills are operator-invokable slash commands loaded via `skills-loader` middleware.

| Skill | Command | Purpose |
|-------|---------|---------|
| `portfolio` | `/portfolio` | Read portfolio state from `.agents/user/portfolio.json`, compute current exposure |
| `watchlist` | `/watchlist` | List operator's watchlist, cross-reference with today's signals |
| `screen` | `/screen [criteria]` | Filter universe by basic criteria (sector, market cap) for strategy development |

**Removed skills** (legacy diligence era): `diligence-checklist`, `comps`, `earnings-delta`, `filing-diff`, `mgmt-scorecard`, `trade`, `scenario`, `morning-note`

---

## 10. Discord Commands Reference

### Zero-Token Commands (Flash Mode)
| Command | Response |
|---------|----------|
| `!john /ping` | Latency check |
| `!john /status` | Full system status (regime, budget, pipeline, next collection) |
| `!john /quote TICKER` | Live price snapshot |
| `!john /profile TICKER` | Company profile from FMP |
| `!john /calendar TICKER` | Next earnings date |
| `!john /market` | Market open/closed + ET time |
| `!john /rate` | Rate limit bucket status per provider |
| `!john /verdict TICKER` | Cached verdict lookup |

### Signal & Strategy Commands
| Command | Response |
|---------|----------|
| `!john /signals` | Today's signal output from DB (zero tokens) |
| `!john /engine-status` | Execution engine last run status |
| `!john /engine-run` | Trigger execution engine manually |
| `!john /strategy-review` | List strategies pending approval |
| `!john /approve-strategy {id}` | Activate a strategy for live signal generation |
| `!john /pause-strategy {id}` | Pause a strategy (skipped by signal runner) |
| `!john /adjust-strategy {base_id} PARAM=value reason: {why}` | Create new versioned copy with adjusted parameters |
| `!john /strategy-versions {base_id}` | Show full version history for a strategy |

### Pipeline Commands
| Command | Response |
|---------|----------|
| `!john /pipeline` | Collection pipeline status |
| `!john /pipeline pause` / `resume` | Pause or resume data collection |
| `!john /coverage` | SP500 data coverage summary |

---

## 11. Database Schema (Key Tables)

| Table | Purpose | Current Size |
|-------|---------|-------------|
| `price_data` | OHLCV time series, all tickers | 73 MB |
| `options_data` | EOD options chain snapshots | 18 MB |
| `market_news` | News feed items | 3.3 MB |
| `fundamentals` | Income, ratios, FCF per ticker | 304 KB |
| `universe_config` | Active ticker universe with metadata | 216 KB |
| `strategy_versions` | Immutable strategy version history | 96 KB |
| `execution_signals` | Signals generated by live strategies | 48 KB |
| `signal_pnl` | Per-signal P&L tracking | 48 KB |
| `verdict_cache` | Cached analysis results (staleness-controlled) | 72 KB |
| `strategy_registry` | All known strategies + approval status | 48 KB |
| `market_regime` | HMM state history | 32 KB |
| `workspaces` | Workspace definitions | 32 KB |
| `pipeline_config` | Runtime configuration (mutable) | 32 KB |
| `research_sessions` | Strategist session history | 48 KB |
| `backtest_results` | Strategy backtests with full metrics | 64 KB |
| `token_usage_log` | Daily LLM token spend by agent type | 24 KB |

---

## 12. Security

- **Secret redaction middleware:** All API keys stripped from LLM context before processing
- **File integrity verification:** SHA-256 manifest check on startup — SSE alert on mismatch
- **Strategy immutability:** chmod 444 on deployed strategy files; versioning enforces audit trail
- **HITL gate:** Operator approval required for any persistent state changes outside workspace
- **Credential sync:** claudebot credentials copied from `/root/.claude/` every 45 minutes

---

## 13. Known Gaps / Pending Work

| Item | Priority | Notes |
|------|----------|-------|
| Signal pipeline runs at 16:15, data collects at 17:00 | Medium | Signals currently run on D-1 data. Move signal pipeline to 17:30 to run on today's close data. Requires operator decision. |
| `data/master/` Parquet files empty | High | Master dataset Parquets not yet populated — `refresh_prices()` has not been run against SP500. First collection run at 17:00 will begin filling. |
| SP500 constituent endpoint (FMP) returns 402 | Low | `/stable/sp500-constituent` requires higher FMP tier. Hardcoded list in use as fallback. Add `/refresh-universe` Discord command when endpoint becomes available. |
| Polygon rate limit still at 5 req/min | Medium | User confirmed increased Massive limits — `polygon_req_per_min` not yet updated in `pipeline_config`. Run: `UPDATE pipeline_config SET value='50' WHERE key='polygon_req_per_min';` after confirming new limit. |
| 17 of 20 strategies not yet discovered | Ongoing | Strategist will populate during off-hours research sessions. Currently 3 deployed. |
| `signal_performance` table has 0 bytes | Expected | No closed trades yet — strategies just registered. Will populate after first live signal cycle. |

---

## 14. File Structure Reference

```
/root/openclaw/
├── src/
│   ├── agent/
│   │   ├── main.js              — PTC mode entry point
│   │   ├── flash.js             — Flash mode (quick queries, <10s)
│   │   ├── prompts/
│   │   │   ├── base.md          — BotJohn master prompt
│   │   │   ├── components/      — 9 injected prompt components
│   │   │   └── subagents/       — 8 agent-specific prompts
│   │   ├── middleware/          — 9 middleware layers
│   │   ├── subagents/           — swarm.js (spawning), types.js, lifecycle.js
│   │   ├── graph/               — workflow.js, strategist-scheduler.js
│   │   ├── config/              — models.js, servers.json, subagent-types.json
│   │   └── tools/
│   │       └── snapshot/        — quote.js, market-status.js, profile.js, earnings-calendar.js
│   ├── channels/discord/        — bot.js, relay.js, notifications.js, setup.js
│   ├── database/                — postgres.js, redis.js, models/, migrations/
│   │   └── migrations/          — 014 migrations (001–014_strategy_versions.sql)
│   ├── engine/
│   │   ├── cron-schedule.js     — heartbeat, all scheduled operations
│   │   └── strategy-version-manager.js  — immutability + versioning
│   ├── pipeline/
│   │   ├── collector.js         — multi-phase data collection
│   │   ├── store.js             — DB read/write helpers
│   │   └── universe.js          — SP500 static list + getUniverse()
│   ├── skills/                  — portfolio/, screen/, watchlist/
│   ├── budget/                  — enforcer.js (token budget)
│   ├── security/                — integrity.js (SHA-256 file hashing)
│   └── execution/               — runner.js (signal execution engine)
├── workspaces/default/
│   ├── strategies/              — mv01, ca02, bs03 (all chmod 444 after v1)
│   │   ├── base.py              — BaseStrategy + SignalResult schema
│   │   └── __init__.py          — auto-discovery loader
│   └── tools/                   — master_dataset.py, signal_runner.py,
│                                   signals_cache.py, backtest.py,
│                                   polygon.py, fmp.py, sec_edgar.py,
│                                   alpha_vantage.py, yahoo.py, tavily.py,
│                                   deployment_validator.py, _rate_limiter.py
├── data/
│   └── master/                  — prices.parquet, financials.parquet,
│                                   options_eod.parquet, macro.parquet, insider.parquet
├── .agents/
│   ├── market-state/            — latest.json, hmm_model_latest.pkl, regime logs
│   └── user/                    — portfolio.json, preferences.json
├── results/
│   └── strategies/              — strategy performance reports
├── scripts/
│   ├── run_market_state.py      — HMM + regime pipeline (cron entry point)
│   └── run_tier_b.py            — legacy (kept for reference)
└── SYSTEM_REPORT.md             — this file
```

---

*This report reflects system state as of 2026-04-10. Intended as a complete technical reference for any agent or operator that needs to understand this system from scratch.*
