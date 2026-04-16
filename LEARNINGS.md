# FundJohn — Shared Learnings & Decisions

> **System**: FundJohn / OpenClaw v2.0
> **Last updated**: 2026-04-16 (HEAD `68a81a1`)
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

## 8. 50% paper sizing rule

Paper and monitoring strategies receive 50% of the normal position size in `trade_agent.py`. Live strategies receive 100% (further scaled by regime).

**Why**: a paper strategy that hasn't cleared the Sharpe/DD gate is not trusted for full size. But zero-sizing means no P&L learning signal during paper; 50% gives a real-money learning signal at half the risk. Once it clears the gate, it promotes to live and gets full size.

**How to apply**: do not add new size tiers. `live=1.0`, `paper|monitoring=0.5`, everything else=0 is the policy. If it's more complex than that, raise it with the operator.

---

## 9. Confluence scoring

`MIN_CONFLUENCE = 2` and `confidence ≥ 0.50` required for high-conviction candidates in `trade_agent.py`.

**Why**: a single strategy firing is noise; two independent strategies firing on the same ticker with the same direction is a signal. This is why we run ~20 strategies and not one big one — we're buying decorrelated alpha streams and then demanding confluence.

**How to apply**: if you add a strategy that correlates too heavily with an existing one (e.g. a second 12-month momentum variant), it pollutes the confluence count. Check correlation against existing live strategies before merging. The manifest has space for audit notes — use them.

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

- **2026-04-16** (`68a81a1`) — wired `pipeline_orchestrator` to cron properly, fixed strategy `generate_signals` signatures across S_HV13-15.
- **2026-04-15** (`19d55fb`) — removed the strategist subagent cron (never used in v2) and made `post_memos` use dynamic strategy labels instead of hardcoded names.
- **2026-04-15** (`ad2a9f2`, `183dba6`) — promoted S_HV17 to paper after earnings_calendar backfill went live.
- **2026-04-14** (`95f7691`, `ef8e2cf`) — DataJohn removal. Single biggest architectural change since reinit.
- **2026-04-14** (`e793742`) — added S_HV13 through S_HV20 (the JFQA/JFE options-literature cohort).
- **2026-04-13** — Audit R3: canonicalised numbered IDs (`S5_max_pain` is canon, `max_pain` is alias); moved five originals to `decommissioned/`; flagged S_custom_momentum_trend_v1 as orphan.

---

## 14. Open questions / tech debt

- **S_HV10 staging** — needs `unusual_flow` data. Either find a provider that exposes this cleanly, or rewrite S_HV10 to use what we have.
- **Backtest harness** — `src/backtesting/` exists but is not integrated with the promotion gate. Sharpe/DD for promotion is currently computed ad-hoc from `signal_performance`.
- **Dashboard auth** — the web dashboard has read-only auth; write actions (pause pipeline, veto strategy) still go through Discord.
- **Legacy data** — `015_data_agent.sql` tables are retained but unused. Decision: archive or drop in a future migration.
- **`orchestrator.js` as manual tool** — should it be formalised as a CLI (`npx diligence <ticker>`) or left as a script?

---

*If a decision in this file no longer makes sense given current state, update it. Don't delete — keep the history inline so future operators can follow the reasoning.*
