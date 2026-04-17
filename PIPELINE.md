# FundJohn — Canonical Pipeline Reference

> **System**: FundJohn / OpenClaw v2.0 — autonomous bot-network hedge fund
> **Last verified**: 2026-04-16 against HEAD `68a81a1`
> **Companion docs**: [ARCHITECTURE.md](ARCHITECTURE.md) · [LEARNINGS.md](LEARNINGS.md) · [README.md](README.md)

This document is the canonical, file-by-file definition of every pipeline that runs in FundJohn. It supersedes any pipeline description in `SYSTEM_REPORT.md` or inline comments. When anything on the VPS changes, this file is the reference that gets updated first.

---

## 1. Daily Cycle (America/New_York)

FundJohn runs on a fixed intraday schedule. All timestamps are ET.

| Time | Component | Triggered by | Tokens | Output |
|---|---|---|---|---|
| Continuous | Market-hours data polling | `src/ingestion/*` + DataPipeline | 0 | Postgres rows |
| **16:20** | **Signal pipeline (zero-LLM)** | `cron.schedule('20 16 * * 1-5')` in `src/engine/cron-schedule.js` → `runMarketClosePipeline()` | 0 | `regime.json`, `signals_cache.parquet`, `signals` table |
| 16:20 (chained) | Strategy memo publisher | `src/execution/runner.js::runDailyClose()` spawned by pipeline orchestrator | 0 | `output/memos/*_{date}.md`, Discord `#strategy-memos` |
| ~16:30 | Research pipeline | Discord `#strategy-memos` post → `bot.js::handleBotMessage()` → `runResearchPipeline()` → `src/execution/research_report.py` | LLM | `output/reports/{date}_research.md`, Discord `#research-feed` |
| ~16:45 | Trade pipeline | `#research-feed` post → `runTradePipeline()` → `src/execution/trade_agent.py` | LLM | `output/signals/{date}_signals.md`, Discord `#trade-signals` + Alpaca paper bracket orders |
| **23:59** | Token-budget reset | `cron.schedule('59 23 * * *')` → `resetTokenBudgets()` | 0 | Redis key cleanup |
| **Sun 08:00** | Weekly memory synthesis | `cron.schedule('0 8 * * 0')` → `swarm.init({type:'strategist', mode:'REPORT'})` | LLM | `agent.md`, `memory/*.md` consolidated |

**Authoritative source**: `src/engine/cron-schedule.js` (lines 51–88 market close, 129–144 budget reset, 237–255 memory synthesis, 258 cron wiring).

The signal pipeline (`post_memos → research_report → trade_agent`) is **not cron-triggered**. It is event-driven through Discord channel posts; the 16:20 cron only runs the zero-LLM pre-work (regime, cache, signal_runner) and then `runner.js::runDailyClose()` posts the first memo, which starts the chain.

---

## 2. The Three Sub-Pipelines

`src/execution/pipeline_orchestrator.py` is the Python supervisor that coordinates the LLM-bearing half of the cycle. It is invoked from `runner.js` after the zero-token signal stage, or re-invoked by `checkPipelineResume()` when the token budget recovers.

```
┌───────────────────────────────────────────────────────────────────────┐
│                     pipeline_orchestrator.py                          │
│                                                                       │
│   Redis lock:  pipeline:running:{YYYY-MM-DD}          (soft fence)    │
│   Checkpoint:  pipeline:resume_checkpoint             (step + date)   │
│   Budget gate: Redis key budget:mode ∈ {GREEN,YELLOW,RED}             │
│                                                                       │
│   steps = [                                                           │
│      ("post_memos",      src/execution/post_memos.py),                │
│      ("research_report", src/execution/research_report.py),           │
│      ("trade_agent",     src/execution/trade_agent.py),               │
│   ]                                                                   │
│                                                                       │
│   Budget gate is checked BEFORE post_memos only.                      │
│   Research + trade run once memos exist (SO-3 research gate).         │
└───────────────────────────────────────────────────────────────────────┘
```

### 2.1 `post_memos.py`

Reads the per-strategy raw signals emitted by `signal_runner.py`, consults the lifecycle manifest, and writes one markdown memo per active strategy to `output/memos/{strategy_id}_{cycle_date}.md`. Each memo carries: lifecycle state (live / paper / staging), regime gating, signal direction + confidence, targets/stops, and the parameters under which the signal fired. The first memo posted to Discord `#strategy-memos` is what wakes ResearchDesk.

### 2.2 `research_report.py`

Reads every memo in `output/memos/*_{cycle_date}.md`, plus today's regime file and recent portfolio state, then produces `output/reports/{cycle_date}_research.md` with six mandated sections: (1) executive summary, (2) regime assessment, (3) per-strategy performance table, (4) convergence / divergence analysis, (5) warnings, (6) recommendation. Emits to `#research-feed`.

### 2.3 `trade_agent.py` — Kelly optimizer + Alpaca paper execution

Reads today's execution signals from Postgres, runs Kelly-criterion optimization across each signal's available exit targets (T1, T2, T3), picks the best (target, Kelly) pair per signal, and posts only **GREEN** signals (`kelly_net > MIN_KELLY`) to `#trade-signals` via TradeDesk. If nothing clears the bar, a "no actionable signals" post with per-signal diagnostics goes out instead.

Key constants (top of `trade_agent.py`):

| Constant | Value | Meaning |
|---|---|---|
| `MAX_POSITION_PCT` | `0.05` | Hard cap per signal (5% of equity) |
| `MIN_KELLY` | `0.005` | Minimum net Kelly to be called actionable |
| `HALF_KELLY` | `0.50` | Safety fraction applied to raw Kelly |
| `CAPTURE_RATIO` | `0.80` | Slippage haircut on reward (80% of target captured) |

Optimization math (`kelly_optimize`):

1. `p_hit_upper(entry, stop, target, mu_daily, sigma_daily)` — GBM two-barrier probability that target is hit before stop, parameterised on SPY-relative drift/vol.
2. For each (T1, T2, T3), compute `R = reward / risk`, `kelly_raw = (p·R - (1-p)) / R`.
3. Apply `kelly_net = kelly_raw * HALF_KELLY`; then `kelly_pos = clip(kelly_net, 0, MAX_POSITION_PCT)`.
4. Best pair per signal = max by `kelly_net`.

**Alpaca paper-trading (added 2026-04-16, commit `9f326f3`)**: immediately after green signals are identified, `execute_alpaca_orders(green, run_date)` submits **bracket orders** (market + take-profit + stop-loss) sized `kelly_pos × equity` to Alpaca. `build_alpaca_post()` appends a condensed order summary to the `#trade-signals` green post. Credentials are read from `.env` at runtime: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL` (point at the Alpaca paper endpoint for paper mode). Orders fire before BotJohn approval — this is **paper** trading by design; promoting to live will add a gate here.

Operator override: BotJohn remains the authority for any *real-money* routing (SO-1..SO-6). The Alpaca execution path is the paper-mode shortcut.

---

## 3. Zero-LLM Signal Stage (16:20 ET)

This is the hot path. Every market close it runs in three steps, all pure Python, no API calls beyond what's already cached:

### Step 1: Market state (`scripts/run_market_state.py`)

Runs the HMM regime classifier and writes `workspaces/default/regime.json`. The regime is one of `LOW_VOL`, `TRANSITIONING`, `HIGH_VOL`, `CRISIS`. Position scaling multipliers live in `src/strategies/base.py::REGIME_POSITION_SCALE` — `1.00 / 0.55 / 0.35 / 0.15` respectively.

### Step 2: Signals cache (`workspaces/default/tools/signals_cache.py --build`)

Rolls up the master dataset into a compact parquet/feather file that every strategy reads. This avoids each strategy re-hitting Postgres for the same OHLCV / options / financials. The cache is rebuilt daily; strategies only read it.

### Step 3: Strategy signal runner (`workspaces/default/tools/signal_runner.py`)

Walks every approved strategy returned by `src/strategies/registry.py::get_approved_strategies()`. For each:

1. Check `should_run(regime_state)` on the strategy instance. Only runs if the current regime is in `active_in_regimes`.
2. Call `generate_signals(prices, regime, universe, aux_data)`.
3. Persist any returned `Signal` objects to Postgres.
4. Log to `signal_performance` with `reported=false` so report triggers can notice.

The runner emits **zero LLM tokens** by construction — it only runs the hardcoded Python strategies listed in `_IMPL_MAP` (registry.py) that are in the `approved` state in the DB row.

---

## 4. Strategy Registry and Lifecycle

### 4.1 Registry wiring (`src/strategies/registry.py`)

`_IMPL_MAP` maps a strategy DB id to `(python_module_path, class_name)`. The IDs fall into three groups:

| Group | Example IDs | Implementation folder |
|---|---|---|
| Canonical live | `S5_max_pain`, `S9_dual_momentum`, `S10_quality_value`, `S12_insider`, `S15_iv_rv_arb`, `S_custom_jt_momentum_12mo` | `src/strategies/implementations/s*_*.py` |
| HV paper (vol-first) | `S_HV7` … `S_HV20` (13 of them — no S_HV18) | `src/strategies/implementations/shv*.py` |
| Alt-form aliases | `max_pain`, `dual_momentum`, `quality_value`, `insider_cluster_buy`, `iv_rv_arb`, `jt_momentum_12mo` | Same modules as canonical live — aliases kept to avoid invalidating historical `signals` rows |

`load_strategy_class(sid)` imports the module lazily and returns the class or `None`. `get_approved_strategies(db_rows)` instantiates only those with `status == 'approved'`, overrides `instance.id` with the DB id, and returns the list. Failed imports are logged but never raise.

### 4.2 Lifecycle state machine (`src/strategies/lifecycle.py`)

`LifecycleStateMachine` enforces strategy promotion. States: `candidate → paper → live → monitoring → deprecated → archived`. Valid transitions live in `VALID_TRANSITIONS`. Promotion gate from `paper → live` requires metadata `sharpe ≥ 0.5` and `max_drawdown ≤ 0.20`; any other transition only needs to exist in the table. History is appended to each strategy record, so every promotion has an audit trail with actor, reason, and timestamp.

### 4.3 Manifest as source of truth (`src/strategies/manifest.json`)

Mirrors the DB lifecycle state for disaster recovery and for humans. Current distribution (as of 2026-04-16):

- **live**: S5_max_pain, S9_dual_momentum, S10_quality_value, S12_insider, S15_iv_rv_arb, S_custom_jt_momentum_12mo
- **paper**: S23_regime_momentum, S24_52wk_high_proximity, S25_dual_momentum_v2, S_HV7/8/9/11/12/13/14/15/16/17/19/20
- **staging**: S_HV10_triple_gate_fear (blocked on unusual_flow data)
- **deprecated**: S_custom_momentum_trend_v1
- **decommissioned** (archived): `dual_momentum_original`, `quality_value_original`, `insider_cluster_buy_original`, `iv_rv_arb_original`, `max_pain_original` — five files moved to `implementations/decommissioned/` during R3 canonicalisation on 2026-04-13

### 4.4 Base class contract (`src/strategies/base.py`)

Every strategy inherits `BaseStrategy(ABC)` and implements `generate_signals(prices, regime, universe, aux_data=None) -> List[Signal]`. Rules enforced by convention (and CI):

1. `generate_signals` is pure Python — no API/LLM calls, no network I/O.
2. Data comes in as pre-loaded DataFrames from the signals cache.
3. Deterministic: same inputs → same outputs (any randomness must be explicitly seeded).
4. Missing data → return `[]`, never raise.

`compute_stops_and_targets()` is a shared ATR-based stop/target helper available to every subclass.

---

## 5. Data Collection Pipeline (continuous, pre-16:20)

Hardcoded — replaced the old DataJohn LLM agent. Target: all collection complete ≤16:20 ET so signals can run.

Primary collector: **`src/ingestion/pipeline.py`** — a 3-layer async ETL.

- **Layer 1 — Fetch.** `aiohttp` with per-provider semaphore rate-limiting. FMP Starter tier gets `semaphore(5)` (300 req/min budget); a secondary "massive" endpoint gets `semaphore(2)` (60 req/min).
- **Layer 2 — Transform.** Normalise raw API responses into `MasterBar` records; compute EMA20, EMA50, RSI(14) from OHLCV history inline.
- **Layer 3 — Cache.** Write to `data/cache/{symbol}/{date}.json` with a 23-hour TTL.

Scheduler: APScheduler, daily 16:20 America/New_York.

Secondary collectors / sources:

- **`src/ingestion/edgar_client.py`** — SEC EDGAR 10-K / 10-Q / 8-K / Form 4 access. Writes to `insider_transactions` (migration 017). Consumed by `S12_insider`.
- **Generated MCP modules** at `workspaces/default/tools/*.py` — each strategy's `aux_data` is pulled from here when a needed field isn't in the `data/cache/` artifact. Tier/fallback ordering comes from `src/agent/config/servers.json`; rate-limiting is enforced by `tools/_rate_limiter.py::_acquire_token(_PROVIDER)`.
- **News + sentiment** — Tavily + Alpha Vantage → `market_news` (migration 009).
- **Macro / regime features** — VIX term structure, realised-vol, RORO inputs → consumed by `scripts/run_market_state.py`.

Provider roster (full table in [ARCHITECTURE.md §6](ARCHITECTURE.md)):

| Provider | Tier | Fallback | Used by |
|---|---|---|---|
| FMP | 1 | Yahoo | OHLCV, financials, ratios, earnings calendar |
| Polygon | 1 | Alpha Vantage | OHLCV, options chain (IV surface, Greeks, OI) |
| SEC EDGAR | 1 | — | Form 4, 10-K/Q/K |
| Tavily | 1 | — | News, transcripts |
| Alpha Vantage | 2 | — | Macro, technical indicators |
| Yahoo | 2 | — | VIX, options chains, insider tx, short interest |

Data dependencies of note: `S_HV8` requires `theta` live in the options chain; `S_HV13–S_HV15` require call/put IV and term-structure fields; `S_HV17` requires `earnings_dte` (wired in commit `183dba6`).

---

## 6. Discord Event Flow

```
 DataBot (runner.js)                ResearchDesk                  TradeDesk
 ─────────────────                  ────────────                  ────────
  #strategy-memos  ────post───▶     listens ──▶  research_report.py
                                                      │
                                                      ▼
                                    #research-feed  ────post───▶  listens
                                                                       │
                                                                       ▼
                                                                 trade_agent.py
                                                                   (Kelly opt.)
                                                                       │
                                                   ┌───────────────────┼─────────────────────┐
                                                   ▼                                         ▼
                                             #trade-signals                         Alpaca (paper)
                                             #trade-reports                         bracket orders
                                                   │
                                                   ▼
                                             BotJohn review
                                             (SO-1..SO-6 gates)
                                                   │
                                                   ▼
                                           live broker route (future)
```

Routing lives in `src/channels/discord/bot.js::handleBotMessage()`. Each channel handler is idempotent; reposting a memo does not re-enter the pipeline if `pipeline:running:{date}` is still held.

---

## 7. Checkpointing & Resume

`pipeline_orchestrator.py` uses two Redis keys:

- **`pipeline:running:{YYYY-MM-DD}`** — soft lock; TTL set to the remaining intraday budget. While held, `checkPipelineResume()` is a no-op. Released when all three steps complete, or cleared at 23:59 by the budget reset.
- **`pipeline:resume_checkpoint`** — JSON `{ run_date, next_step }`. Written after every successful step. If the orchestrator aborts mid-pipeline (budget RED, crash, or manual stop) this is the resume point.

`src/engine/cron-schedule.js::checkPipelineResume()` runs every 30 minutes during off-hours. It re-spawns the orchestrator with `--force-resume` only if: (a) checkpoint exists, (b) lock is not held, (c) `budget:mode ≠ 'RED'`. Under RED the pipeline holds at its checkpoint until spend drops below the RED threshold (or the month rolls over).

---

## 8. Budget Governance (dollar-based)

Managed by **`src/budget/enforcer.js`** + `config/budget.json` + Redis. Modes are computed from *dollar* spend, not token count. Defaults (overridable via `config/budget.json`):

```
monthly_budget_usd:     400
daily_burn_yellow_usd:   20
daily_burn_red_usd:      35
monthly_pct_yellow:      75   (% of monthly budget)
monthly_pct_red:         90
```

Mode rules (evaluated by `checkBudget()`):

| Mode | Trigger (any) | Effect |
|---|---|---|
| GREEN | else | All phases run |
| YELLOW | daily ≥ $20 or month ≥ 75% of $400 | Skip Phase 6 (news); reduce Phase 5 (fundamentals) to weekly (Sundays only) |
| RED | daily ≥ $35 or month ≥ 90% of $400 | Price collection only; all PTC (plan-then-commit) ops require manual trigger |

Redis keys (TTL 1 h, refreshed each cycle):

- `budget:mode` — current mode string
- `budget:daily_usd` — today's spend
- `budget:monthly_usd` — 30-day trailing spend

`pipeline_orchestrator.py` reads `budget:mode` before `post_memos` and before `checkPipelineResume()` re-spawns a paused orchestrator. `swarm.init()` reads it before spawning any subagent. `bot.js` reads it before replying to `@FundJohn`.

Per-agent $ caps and iteration limits (independent of the mode gate) are in `src/agent/config/subagent-types.json` — e.g. BotJohn `maxBudgetUsd: 1.00` at 40 iterations; Research/Trade `maxBudgetUsd: 0.30` at 15 iterations each.

---

## 9. Report Triggers (post-cycle)

After the zero-LLM stage, `cron-schedule.js::checkReportTriggers()` queries:

```sql
SELECT strategy_id,
       COUNT(*) FILTER (WHERE pnl_pct IS NOT NULL)                        AS completed,
       COUNT(*) FILTER (WHERE pnl_pct IS NOT NULL AND NOT reported)       AS unreported
FROM   signal_performance
WHERE  workspace_id = $1
GROUP  BY strategy_id
HAVING COUNT(*) FILTER (WHERE pnl_pct IS NOT NULL AND NOT reported) >= 30;
```

Any strategy with 30+ unreported completed trades is queued at `queue:report:{workspace_id}`. The queue is consumed by `processReportQueue()`, which spawns a `report-builder` subagent in `STRATEGY_PERFORMANCE` mode.

---

## 10. File Map — Pipeline Surface

| File | Language | Role |
|---|---|---|
| `src/engine/cron-schedule.js` | Node | Cron jobs + report-trigger + resume poller |
| `src/execution/runner.js` | Node | `runDailyClose()` — spawns engine.py, posts memos |
| `src/execution/engine.py` | Python | Master execution engine — signal orchestration |
| `src/execution/pipeline_orchestrator.py` | Python | Sequences memos → research → trade with checkpointing |
| `src/execution/post_memos.py` | Python | Per-strategy memo writer |
| `src/execution/research_report.py` | Python | Aggregated research doc |
| `src/execution/trade_agent.py` | Python | Confluence scoring + sizing → signal doc |
| `src/execution/send_report.py` | Python | Pushes strategy performance reports |
| `src/strategies/registry.py` | Python | `_IMPL_MAP` + `get_approved_strategies()` |
| `src/strategies/lifecycle.py` | Python | `LifecycleStateMachine` + manifest I/O |
| `src/strategies/manifest.json` | JSON | Lifecycle source-of-truth mirror |
| `src/strategies/base.py` | Python | `BaseStrategy` ABC + `Signal` dataclass + regime scales |
| `src/strategies/implementations/*.py` | Python | 24 strategy implementations |
| `scripts/run_market_state.py` | Python | HMM regime classifier |
| `workspaces/default/tools/signals_cache.py` | Python | Dataset rollup cache builder |
| `workspaces/default/tools/signal_runner.py` | Python | Zero-LLM strategy executor |
| `src/channels/discord/bot.js` | Node | Discord event router → research/trade pipelines |

---

## 11. Invariants Worth Not Breaking

These are rules that, if violated, break the pipeline in non-obvious ways:

1. **Data collection must complete before 16:20 ET.** If a provider is degraded, let the collector fail loudly rather than feed stale data to `signals_cache`.
2. **Strategies never import from `agent/` or `channels/`.** A strategy must be runnable standalone in a Python REPL with only a DataFrame. This is what keeps the signal stage at zero tokens.
3. **`generate_signals` is deterministic.** Any RNG must be seeded. Non-determinism here leaks into backtests and breaks promotion.
4. **Manifest and DB must agree.** `manifest.json` is the recovery artifact; the `strategy_registry` Postgres table is operational truth. Both are updated by `lifecycle.save_manifest()` after every transition.
5. **`pipeline:resume_checkpoint` takes precedence over `budget:mode == RED`.** Once written, resume only happens when mode is not RED — do not add a branch that ignores this.
6. **Aliases in `_IMPL_MAP` are historical.** Do not remove `max_pain`, `dual_momentum`, etc. — existing `signals` rows reference them. Only add new IDs, never rename.

---

*This document is the canonical pipeline description. If an operator finds it disagrees with what's running on the VPS, this file is wrong and should be corrected before the code is touched.*
