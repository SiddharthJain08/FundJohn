# FundJohn — Shared Learnings & Decisions

> **System**: FundJohn / OpenClaw v2.0
> **Last updated**: 2026-04-18 (HEAD `beea4cd`)
> **Companion docs**: [PIPELINE.md](PIPELINE.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [README.md](README.md)

This file records *why* FundJohn is built the way it is. When a future operator asks "should we bring back DataJohn?" or "why can't strategies call the MCP tools directly?", the answer is here. Each entry has a **decision**, a **why**, and **how to apply** — so the next person can judge whether the reasoning still holds.

---

## 1. Evolution: v1 → v2 → v2.1

### v1 — Diligence swarm (pre-reinit)

The earliest FundJohn was a per-ticker diligence orchestrator (`scripts/orchestrator.js`). BotJohn would spawn 5 sub-agents in parallel per ticker, each returning a `---AGENT:id---` structured block, and assemble a 12-section memo. Verdict came from a deterministic 6-item checklist.

**Why it was retired**: every run was expensive (5× parallel LLM calls × ~15 tickers), and the verdict logic ran entirely after LLM work, so there was no way to stop early when a thesis clearly failed on item 1 of 6. It also had no lifecycle — every ticker was evaluated from scratch daily.

**What remains**: `scripts/orchestrator.js` is preserved as a manual deep-dive tool. Useful when an operator wants a point-in-time memo on a single ticker outside the daily cycle.

### v2 — 4-agent (BotJohn + DataJohn + ResearchJohn + TradeJohn)

Introduced the split between data, research, and trade. DataJohn was an LLM agent responsible for fetching OHLCV / options / fundamentals and writing them to Postgres.

**Why it was retired**: DataJohn burnt ~60% of the daily token budget on tasks that were deterministic (call API, parse JSON, write rows). It introduced non-determinism into a stage that had no business being non-deterministic. See §3.

### v2.1 — 3-agent + hardcoded DataPipeline (current)

Removed DataJohn in commit `ef8e2cf`, replaced by `src/ingestion/*` hardcoded collectors plus the zero-LLM signal stage at 16:20 ET. BotJohn, ResearchJohn, TradeJohn remain.

---

## 2. Core design principles

### 2.1 "Strategies must be dumb"

Every file under `src/strategies/implementations/` must run in a Python REPL with only a pandas DataFrame. No network, no DB, no LLM, no filesystem writes. The base class (`src/strategies/base.py`) enforces this by contract:

1. `generate_signals` is pure Python.
2. Data comes in pre-loaded.
3. Deterministic: same inputs → same outputs.
4. Missing data → empty list, never raise.

**Why**: determinism is the foundation of backtesting, promotion, and audit. A strategy that calls an API during `generate_signals` is un-backtestable — its historical signals can no longer be reconstructed. Making them dumb is what lets us run them at zero tokens.

**How to apply**: any PR that adds an `import requests` or `import openai` to an `implementations/*.py` file is an auto-reject. If a strategy needs new data, add it to `signals_cache.py` and thread it through `aux_data`.

### 2.2 "Dollars are a resource, budget them"

Every LLM call's **dollar cost** is accounted in Postgres (`src/database/tokens.js::getTotalSpend`), rolled into daily + monthly totals, and reduced to a single Redis key `budget:mode ∈ {GREEN, YELLOW, RED}`. Default thresholds in `src/budget/enforcer.js`: monthly budget $400; YELLOW at $20/day or 75% of month; RED at $35/day or 90% of month. YELLOW skips the news phase and weekly-caps fundamentals; RED restricts to price collection only and blocks plan-then-commit (PTC) operations until the operator manually overrides. `checkPipelineResume()` only re-spawns a paused orchestrator when mode is not RED.

**Why**: a runaway agent is a financial and operational risk. Dollar-based (rather than token-based) budgeting maps directly to the real resource we care about and is robust to model price changes.

**How to apply**: if you add a new agent or subagent type, give it `maxBudgetUsd` in `subagent-types.json` and verify it doesn't exceed the iteration cap. Use the fast model (haiku) for anything that's just summarising or pruning. If you need to raise the monthly limit, edit `config/budget.json` — don't hack the thresholds in code.

### 2.3 "Hardcode determinism; LLM the judgement"

The signal stage is zero LLM. The research and trade stages are LLM. This split is deliberate: signals from hardcoded strategies are reproducible; the *judgement* about which signals to act on today, given regime and portfolio context, is the kind of reasoning an LLM is better at than a lookup table.

**How to apply**: if you find yourself wanting an LLM to decide a price level or a stop, something is wrong. Prices are hardcoded. Prose is LLM.

### 2.4 "Standing Orders before decisions"

SO-1 through SO-6 in `AGENTS.md` are not suggestions. Every agent prompt has them inlined at the top. BotJohn enforces SO-4 (negative-EV veto) before even writing a memo.

**How to apply**: if an agent is doing something that "feels wrong", check the SOs. If the SOs don't cover it, that's a signal to add a new one, not to override.

---

## 3. DataJohn removal (2026-04-14, commit `ef8e2cf`)

### What we removed

The `datajohn` subagent type, its prompts, and all `TIER_A` / `TIER_B` data-prep modes. Agent directory `agents/datajohn/*` kept as a historical artifact (marked DEPRECATED).

### Why

| Symptom | Root cause |
|---|---|
| ~60% of daily token budget spent on data fetching | LLM was re-deciding "which endpoint to call" every cycle when the answer was always the same |
| Intermittent data gaps with no retry logic | LLM would "decide" the endpoint failed and move on silently; a hardcoded collector would've retried with backoff |
| Strategies occasionally saw stale rows | Context window limits caused DataJohn to skip some tickers; hardcoded collectors iterate the universe exhaustively |
| Non-determinism in the signal stage | Two runs with the same inputs could produce slightly different datasets because DataJohn's ordering varied |

### How we replaced it

A hardcoded 3-layer async ETL at `src/ingestion/pipeline.py` plus an EDGAR client at `src/ingestion/edgar_client.py`. The ETL uses `aiohttp` with per-provider semaphore rate-limiting (FMP Starter: 300 req/min · semaphore(5); a secondary "massive" endpoint: 60 req/min · semaphore(2)), normalises into a `MasterBar` row, computes EMA20 / EMA50 / RSI(14) inline, and caches to `data/cache/{symbol}/{date}.json` with a 23-hour TTL. Scheduled daily at 16:20 ET via APScheduler. The zero-LLM signal stage reads this cache.

Falling back between MCP providers (e.g. Polygon → Alpha Vantage, FMP → Yahoo) is still wired through `src/agent/config/servers.json` and the generated `workspaces/default/tools/*.py` modules — those generated modules are what the *strategies* pull from during the signal runner. `src/ingestion/pipeline.py` is the primary collector; the generated MCP modules are used when strategies need something that isn't in the cache.

### How to apply

**Do not** re-introduce an LLM data collector. If a new data source is needed, add a new `src/ingestion/<source>.py` collector and wire it into `signals_cache.py`. If an LLM judgement is needed about which of several sources to prefer (e.g. "if FMP and Polygon disagree on the close price, which do we use"), hardcode the policy; don't delegate to an agent.

---

## 4. Immutable strategy rule

### Decision

Once a strategy has a DB id and `signal_performance` rows, its implementation is immutable. To change it, write a new strategy with a new id (`S25_dual_momentum_v2` is the template) and run both in parallel in paper until the new one is promoted.

### Why

Backtests and promotion gates (SO-5 max-DD escalation) assume historical signals are reproducible from historical inputs. If we edit `S9_dual_momentum.py` in place, yesterday's signals are no longer reconstructible, and the promotion gate sees a time series that mixes two different implementations.

### How to apply

- New behaviour → new id (follow the `SXX_` pattern for factor/classic strategies, `S_HV*` for vol strategies).
- Old id stays pinned to its file forever.
- Aliases in `_IMPL_MAP` (`max_pain`, `dual_momentum`, etc.) exist because the original IDs are still referenced by `signals` table rows from 2025 — those aliases are frozen.
- When a strategy is retired, move the file to `implementations/decommissioned/` and add a row to `manifest.json::decommissioned` with `original_file`, `replaced_by`, `canonical_file`, and `reason`.

---

## 5. Regime gating over universe filtering

### Decision

Strategies declare `active_in_regimes` (default: `['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']`). The runner checks `should_run(regime_state)` before calling `generate_signals`. No strategy runs in `CRISIS` by default — operator must opt a strategy in.

### Why

In 2025 we tried universe filtering (ban certain tickers in HIGH_VOL). It didn't work because most of the tail-risk came from *correlation regime* not from individual names. Gating at the strategy level is cleaner: a strategy that has no business running in HIGH_VOL (e.g. a mean-reversion factor) just opts out.

### How to apply

New strategies: default `active_in_regimes = None` which expands to `['LOW_VOL','TRANSITIONING','HIGH_VOL']`. Explicitly exclude `CRISIS`. Sizing is further scaled by `REGIME_POSITION_SCALE` in `trade_agent.py`.

---

## 6. Event-driven pipeline over cron-chained pipeline

### Decision

The 16:20 cron runs only the zero-LLM part (regime, cache, signal runner). The LLM-bearing chain (memos → research → trade) fires **event-driven** from Discord channel posts.

### Why

Two reasons:

1. If research fails or budget goes RED between memos and research, we want to pause cleanly and resume when the budget recovers. Cron chains don't express that well; a lock + checkpoint + event listener does.
2. Operators can inject manual steps mid-pipeline. E.g. "I want to add context to the research prompt today" → drop a message in `#strategy-memos` before the memo is posted, and ResearchJohn will see it.

### How to apply

- Do not add another cron job that chains after `runMarketClosePipeline`. Let Discord events drive everything LLM-bearing.
- New event types go through `bot.js::handleBotMessage()` — keep the routing table there flat and easy to read.
- If you need a schedule-triggered LLM task, model it like the weekly memory synthesis (`cron.schedule('0 8 * * 0', ...)` calling `swarm.init`) rather than embedding it in the daily pipeline.

---

## 7. Deprecated modes

Modes that used to be permitted but have been removed from `deployment-gate.js::PERMITTED_MODES`:

| Mode | Removed | Replaced by |
|---|---|---|
| `TIER_A` | 2026-04-14 | Hardcoded ingestion |
| `TIER_B` | 2026-04-14 | Hardcoded ingestion |
| `PER_TICKER_DILIGENCE` | 2026-03 | `scripts/orchestrator.js` as manual tool |
| `DIRECT_RESEARCH` | 2026-03 | `research_report.py` via `#strategy-memos` event |

Current `PERMITTED_MODES`: `DEPLOY`, `REPORT`, `SIGNAL_PROCESSING`, `MARKET_STATE`, `RISK_SCAN`, `PM_TASK`. Anything else is rejected by `deploymentGateMiddleware`.

---

## 8. Sizing is Kelly-based, not tier-based

Position sizing in `trade_agent.py` is pure Kelly criterion with a hard cap: `kelly_net = kelly_raw × HALF_KELLY (0.50)`, then `kelly_pos = clip(kelly_net, 0, MAX_POSITION_PCT=0.05)`. Only signals with `kelly_net > MIN_KELLY (0.005)` post to `#trade-signals`.

**Why**: Kelly gives a principled, EV-maximising sizing that scales with the probability edge of each signal. Half-Kelly caps downside volatility at the cost of some growth, which is the right trade for a paper-scale fund. A flat "1% base + confluence adder" scheme would ignore the actual edge per signal.

**How to apply**: paper vs live lifecycle state is *not* a sizing lever in the current code — if it should be, wire it explicitly (e.g. a `SIZE_MULT_PAPER = 0.5` constant applied before the cap). Don't assume it's in there. Regime scaling *does* happen, but upstream in `BaseStrategy.regime_position_scale()` (LOW_VOL 1.00 / TRANSITIONING 0.55 / HIGH_VOL 0.35 / CRISIS 0.15), not inside `trade_agent.py`.

---

## 9. Confluence detection lives in `engine.py`, not `trade_agent.py`

Cross-strategy confluence is detected by `src/execution/engine.py::detect_confluence()` — when ≥ `CONFLUENCE_MIN (2)` strategies agree on the same ticker + direction, a row is written to the `confluence_signals` table with `combined_size_pct` and the list of agreeing strategies. `trade_agent.py` consumes signal rows (including confluence metadata) but does not itself compute confluence.

**Why**: confluence is a property of the signal *set*, not of individual sizing decisions. Putting it in the engine — which runs once per cycle over all strategies — makes it O(1) per run. Putting it in `trade_agent.py` would couple it to Kelly sizing and make it harder to reason about.

**How to apply**: if you add a strategy that correlates too heavily with an existing one (e.g. a second 12-month momentum variant), it pollutes the confluence count. Check correlation against existing live strategies before merging. If you need to change the confluence threshold, it's `CONFLUENCE_MIN` at the top of `engine.py`.

---

## 10. Manifest + DB dual-write

`lifecycle.save_manifest()` writes both the JSON manifest and the DB `strategy_registry` table on every transition. The manifest is the recovery artifact; the DB is operational truth.

**Why**: if the DB container is lost, we can rehydrate from the manifest. If the manifest drifts, the DB is canonical. Having both means we can survive either failure.

**How to apply**: never hand-edit the manifest and the DB separately. Always go through `LifecycleStateMachine.transition()` — it handles both.

---

## 11. Why we're on Opus for BotJohn and Sonnet for the rest

- **BotJohn** (Opus) — the only agent with veto authority and portfolio access. The cost of a bad decision is high; the cost per token is high but worth it.
- **ResearchJohn + TradeJohn** (Sonnet) — well-defined, narrow inputs, deterministic output format. Sonnet handles this cleanly and cheaply.
- **Compaction / pruning** (Haiku) — summarising turns is the cheapest LLM task in the stack.

If this changes — e.g. a cheaper reasoning model becomes available — update `src/agent/config/models.js` in one place. Every other file reads `MODELS.orchestrator`, `MODELS.primary`, `MODELS.fast` indirectly.

---

## 12. Common pitfalls (gotchas)

### Cron timezones

All cron jobs in `cron-schedule.js` must pass `{ timezone: 'America/New_York' }`. Without it, they run in the VPS's UTC and the 16:20 ET signal job fires at 12:20 ET in DST, or 11:20 ET outside DST. If you add a new job and forget the timezone, it will silently run at the wrong hour.

### Workspace vs repo

The repo lives at `/root/openclaw`. The *runtime workspace* is `/root/openclaw/workspaces/default`. Generated tool modules, memos, reports, signals all go into the workspace — not the repo. The workspace is gitignored. Don't try to read memos from the repo.

### `claude-bin` runs as uid 1001

Spawned subagents cannot read root-owned files outside `/root/openclaw`. If you need a subagent to see a file, put it under the workspace and make sure permissions are `claudebot`-readable.

### Migrations don't auto-run on existing Postgres

`docker-entrypoint-initdb.d` only applies on a *fresh* Postgres volume. For in-place migrations on a running DB, run the `.sql` manually against the live container. Add destructive migrations (like `020_drop_technicals.sql`) with a manual backup first.

### The pipeline lock can wedge

If `pipeline_orchestrator.py` crashes hard between acquiring `pipeline:running:{date}` and releasing it, the lock sits until 23:59 ET reset. Manual unwedge: `redis-cli DEL pipeline:running:YYYY-MM-DD` then re-trigger via Discord.

### Strategy classes load lazily

`load_strategy_class(sid)` catches all exceptions and returns `None`. A typo in `_IMPL_MAP` fails silently — the strategy just doesn't run, with a WARNING log. Always check `validate_all()` output after editing the map.

### Numeric-prefix vs underscore IDs

`S5`, `S9`, `S10` etc. are the *current* convention. Historical IDs without the `S` prefix (`max_pain`, `dual_momentum`) are aliases kept for row compatibility. Don't use aliases for new work.

---

## 13. What changed recently (for context)

- **2026-04-18** (`beea4cd`) — **Massive WebSocket integration, dashboard portfolio page, data pipeline cleanup**:
  - `src/ingestion/massive_ws.py` — `MassiveWSClient` + `MassiveOptionsCapture`. Subscribes `OA.*`, parses OCC symbols, accumulates session vol vs prev-day OI, writes `massive:flow:{underlying}` Redis keys. Runs as `massive-ws.service`.
  - `src/ingestion/massive_client.py` — options-only S3 client. Removed all stock download code (`download_stock_day_bars` deleted). `probe_access()` now uses `list_available_dates()` to find a real available date (avoids holiday 403s).
  - `workspaces/default/tools/master_dataset.py` — `refresh_prices_bulk()` switched to yfinance (`yf.download()` with MultiIndex reshape). Polygon/Massive stock calls removed entirely.
  - `src/pipeline/collector.js` — Phase 2 hardcoded to yfinance only (`usePolygonOHLCV = false`). Phase 3 (options) unchanged, correctly uses Polygon options snapshot REST.
  - `src/ingestion/pipeline.py` — `fetch_polygon_flow()` checks `massive:flow:{symbol}` Redis cache first (< 2h). `sync_universe_to_db()` simplified to FMP only (Polygon universe sync removed).
  - `src/channels/api/server.js` — full Portfolio page: Alpaca account row (equity, cash, day P&L, invested), strategy stats row, 90d P&L curve with P&L%/Value$ toggle, Active Positions + Closed Trades tables. SSE `market_update` event wired end-to-end. Position sizing `%` fix (fractions × 100). Scroll architecture: `#portfolio-page` uses `position:absolute;inset:0;overflow-y:auto` in `#view-wrap`; content in `#pf-inner` (flex column, no height constraint).
  - `src/engine/cron-schedule.js` — WS-aware gate at top of `runMarketClosePipeline()` (skips if `massive_ws:pipeline_fired_today` set); broadcasts `market_update` via HTTP POST to dashboard at the end.
  - `src/execution/alpaca_trader.py` — `execute_alpaca_orders` and `build_alpaca_post` fully implemented (resolves open question from `9f326f3`).
  - 27 DB migrations, `core/` signal pipeline with correlation gate (`CORR_THRESHOLD=0.75`) + concentration gate (`MAX_POSITIONS=8`), 7 subagent types in `subagent-types.json`, strategy plugins framework.
- **2026-04-16** (`9f326f3`) — Alpaca paper trading wired into TradeJohn. Call sites added (bodies added in `beea4cd`).
- **2026-04-16** (`d9f86fe`) — full pipeline wiring + new strategy integration.
- **2026-04-16** (`68a81a1`) — wired `pipeline_orchestrator` to cron, fixed `generate_signals` signatures across S_HV13-15.
- **2026-04-15** (`19d55fb`) — removed strategist subagent cron (never used in v2), dynamic strategy labels in `post_memos`.
- **2026-04-15** (`ad2a9f2`, `183dba6`) — promoted S_HV17 to paper after earnings_calendar backfill went live.
- **2026-04-14** (`95f7691`, `ef8e2cf`) — DataJohn removal. Single biggest architectural change since reinit.
- **2026-04-14** (`e793742`) — added S_HV13 through S_HV20 (the JFQA/JFE options-literature cohort).
- **2026-04-13** — Audit R3: canonicalised numbered IDs (`S5_max_pain` is canon, `max_pain` is alias); moved five originals to `decommissioned/`; flagged S_custom_momentum_trend_v1 as orphan.

---

## 14. Open questions / tech debt

- **Paper vs live sizing lever** — currently no sizing discount for paper-state strategies in `trade_agent.py`. If we want one, add an explicit `SIZE_MULT_PAPER` constant and apply pre-cap.
- **BotJohn approval bypass for Alpaca** — Alpaca paper orders fire automatically after green-signal identification, ahead of any BotJohn review. Fine for paper, must be gated before live-broker routing is wired.
- **S_HV10 staging** — `unusual_flow` Redis key (`massive:flow:{underlying}`) is now written by `MassiveOptionsCapture` during market hours. S_HV10 can be promoted from staging to paper once the WS has been live for a full cycle and the data quality is confirmed.
- **Backtest harness** — `src/backtesting/` exists but is not integrated with the promotion gate. Sharpe/DD for promotion is currently computed ad-hoc from `signal_performance`.
- **Dashboard auth** — the web dashboard has read-only auth; write actions (pause pipeline, veto strategy) still go through Discord.
- **Legacy data** — `015_data_agent.sql` tables are retained but unused. Decision: archive or drop in a future migration.
- **`orchestrator.js` as manual tool** — should it be formalised as a CLI (`npx diligence <ticker>`) or left as a script?
- **Massive WS reconnect on holiday** — `MassiveWSClient` reconnects on disconnect with exponential backoff (`RestartSec=10` in systemd). On market holidays there are no `OA.*` events; the connection stays open with no data. `_prev_oi` is not refreshed intraday — it loads from the last available `options_eod.parquet` at service start.

---

## 15. Massive/Polygon is options-only

### Decision

`MASSIVE_SECRET_KEY` / `POLYGON_API_KEY` (same value) is on the "options starter" plan. This plan authorises:
- Options S3 flat files (`us_options_opra/day_aggs_v1/{Y}/{M}/{date}.csv.gz`)
- Options WebSocket (`wss://socket.massive.com/options`, `OA.*` feed)

It does **not** authorise:
- Stock S3 flat files (`us_stocks_sip`) — 403 on every GetObject
- Stock WebSocket (`wss://socket.massive.com/stocks`) — "Your plan doesn't include websocket access"
- Polygon stock REST grouped/daily bars for the full universe

### Why this matters

If you try to use Polygon for stock prices, every attempt either returns a 403 or is limited to a tiny universe on the free tier. We switched to yfinance bulk download for stocks:

```python
# master_dataset.py::refresh_prices_bulk
raw = yf.download(universe, start=trade_date, end=end_date, auto_adjust=True, progress=False, threads=True)
if isinstance(raw.columns, pd.MultiIndex):
    df = raw.stack(level=1, future_stack=True).reset_index()
    df.columns = [str(c).lower() for c in df.columns]
    df = df.rename(columns={'level_1': 'ticker', 'date': '_dt'})
```

### How to apply

- Stock OHLCV: always yfinance. Do not route to Polygon/Massive.
- Options (chain, IV, OI, flow): always Polygon/Massive. Do not route to Yahoo for options data that Massive has.
- If the plan is upgraded (stocks tier), update the `probe_access()` check in `massive_client.py` and re-enable `collector.js` Phase 2 to attempt Polygon before yfinance.

---

## 16. Dashboard portfolio page — scroll architecture

### Problem

`overflow-y:auto` on a flex child of `<body>` is unreliable across browsers because:
1. Some browsers treat `<body>` specially for overflow (can transfer to viewport)
2. `flex:1` alone doesn't constrain the element height unless `min-height:0` is also set
3. Even with `min-height:0`, an absolutely-positioned overlay is more reliable than a flex-height-constrained scroll container

### Solution

```css
/* View wrapper: positions children, gives them a guaranteed height */
#view-wrap { flex: 1; position: relative; overflow: hidden; min-height: 0; }

/* Market view: fills view-wrap exactly */
#body { position: absolute; inset: 0; display: flex; overflow: hidden; }

/* Portfolio page: scroll container, separate from flex layout */
#portfolio-page { display: none; position: absolute; inset: 0; overflow-y: auto; overflow-x: hidden; background: var(--bg); }

/* Portfolio content: flex column, free to grow beyond #portfolio-page height */
#pf-inner { display: flex; flex-direction: column; gap: 16px; padding: 20px 24px; }
```

The key insight: **the scroll container (`#portfolio-page`) and the flex layout container (`#pf-inner`) are different elements**. `#portfolio-page` has a fixed height (via `position:absolute;inset:0`) and scrolls. `#pf-inner` has no height constraint and grows with content. This pattern works reliably in all browsers.

### How to apply

If you add a new "full-page" view alongside Market and Portfolio:
1. Add a new `position:absolute;inset:0;overflow-y:auto` div inside `#view-wrap`
2. Add an inner wrapper div for the flex/block layout
3. Toggle `display:none`/`display:block` via JS — no need to compute heights

---

*If a decision in this file no longer makes sense given current state, update it. Don't delete — keep the history inline so future operators can follow the reasoning.*
