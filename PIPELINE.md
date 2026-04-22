# FundJohn — Canonical Pipeline Reference

> **System**: FundJohn / OpenClaw v2.1 — autonomous bot-network hedge fund
> **Last verified**: 2026-04-22 against the Phase 2–4 restructure
> **Companion docs**: [ARCHITECTURE.md](ARCHITECTURE.md) · [CLAUDE.md](CLAUDE.md) · [README.md](README.md)

This document supersedes any pipeline description in `SYSTEM_REPORT.md` or
inline comments. When the VPS changes, update this file first.

---

## 1. Daily Cycle (America/New_York)

A single 10:00 AM ET cron drives the full cycle. All LLM work lives inside
one step (trade) so the budget envelope is predictable.

| Time | Component | Triggered by | Tokens | Output |
|---|---|---|---|---|
| Continuous | Market-hours snapshot polling | `src/pipeline/collector.js` 5-min interval | 0 | `prices.parquet` live tick |
| **09:00** | Morning regime refresh | `cron.schedule('0 9 * * 1-5')` → `scripts/run_market_state.py` | 0 | `workspaces/default/regime.json` |
| **10:00** | **Daily orchestrator** | `cron.schedule('0 10 * * 1-5')` → `scripts/run_pipeline.py` → `pipeline_orchestrator.py` | LLM (trade only) | See §2 |
| **20:00 Fri** | MastermindJohn strategy-stack memo | `openclaw-mastermind-weekly.timer` → `run_mastermind.js --mode strategy-stack` | Opus 4.7 | `#strategy-memos` + `#position-recommendations` + `mastermind_weekly_reports` |
| **23:59** | Token-budget reset | `cron.schedule('59 23 * * *')` | 0 | Redis key cleanup |
| **Sat 10:00** | MastermindJohn corpus curation | `openclaw-mastermind-corpus.timer` → `run_mastermind.js --mode corpus` | Opus 4.7 | `curated_candidates` + `research_candidates` |
| **Sun 08:00** | Weekly memory synthesis + reaper | `cron.schedule('0 8 * * 0')` | LLM | `agent.md`, `memory/*.md`, `data_deprecation_queue` |

Authoritative cron definitions: `src/engine/cron-schedule.js`.

---

## 2. The 10:00 AM orchestrator

`src/execution/pipeline_orchestrator.py` runs seven steps in order. All
state is in Redis + Postgres; mid-cycle aborts resume from checkpoint.

```
┌───────────────────────────────────────────────────────────────────────┐
│                     pipeline_orchestrator.py                          │
│                                                                       │
│   Redis lock:       pipeline:running:{YYYY-MM-DD}                     │
│   Idempotency:      pipeline:completed:{YYYY-MM-DD}                   │
│   Checkpoint:       pipeline:resume_checkpoint                        │
│   Budget gate:      Redis key budget:mode — checked BEFORE `trade`    │
│                                                                       │
│   steps = [                                                           │
│     ('queue_drain', src/pipeline/queue_drain.py),        # 0 LLM      │
│     ('collect',     src/pipeline/run_collector_once.js), # 0 LLM      │
│     ('signals',     src/execution/engine.py),            # 0 LLM      │
│     ('handoff',     src/execution/trade_handoff_builder.py), # 0 LLM  │
│     ('trade',       src/execution/trade_agent_llm.py),   # TradeJohn  │
│     ('alpaca',      src/execution/alpaca_executor.py),   # 0 LLM      │
│     ('report',      src/execution/send_report.py),       # 0 LLM      │
│   ]                                                                   │
│                                                                       │
│   After all 7 steps: broadcast_dashboard_refresh() → SSE fan-out.     │
└───────────────────────────────────────────────────────────────────────┘
```

Per-step pipeline-feed post (▶️ start → ✅/❌ end in Ns) to
`#pipeline-feed` for operator visibility — non-blocking.

### 2.1 `queue_drain`

`src/pipeline/queue_drain.py` drains two tables:

- **`data_ingestion_queue`** (column additions approved in the dashboard):
  `status='APPROVED'` AND `backfill_status IN ('pending','failed')` →
  dispatch to `src/pipeline/backfillers/{provider}.py` over the declared
  `backfill_from`/`backfill_to` window → `backfill_status='complete'`
  on success. Provider is resolved by `provider_preferred` → `data_columns`
  ledger → `schema_registry.json` datasets.
- **`data_deprecation_queue`** (unstack-triggered removals):
  `status='APPROVED'` AND `deletion_applied_at IS NULL` → remove from
  `schema_registry.json` + `data_columns` so the next `collect` step
  skips the column. Historical parquet data preserved.

Progress posts to `#data-alerts`.

### 2.2 `collect`

`src/pipeline/run_collector_once.js` invokes `collector.runDailyCollection()`
— one synchronous cycle against the configured universe. Writes directly
to master parquets (`prices.parquet`, `options_eod.parquet`,
`fundamentals.parquet`, `insider.parquet`, `macro.parquet`). Skips
already-current datasets via the freshness scan.

### 2.3 `signals`

`src/execution/engine.py` runs the zero-LLM signal executor:
- Load regime + approved strategies (`src/strategies/registry.py`).
- Load signals_cache (workspace parquet). Run each strategy's
  `generate_signals(prices, regime, universe, aux_data)`.
- Persist signals to `execution_signals`.
- Detect confluence (≥2 strategies agreeing on same ticker/direction).
- Update P&L on open signals; fire report triggers on 30+ unreported
  completed trades.

### 2.4 `handoff`

`src/execution/trade_handoff_builder.py` — deterministic feature builder.
Reads `execution_signals` for `run_date`, computes per-signal HV21/63/252,
beta-to-SPY, momentum at 1m/3m/6m/12m, RSI14, and GBM two-barrier
EV/p(T1) (overflow-guarded). Writes `handoff:{run_date}:structured`:

```jsonc
{
  "cycle_date": "...",
  "regime":   { "state": "...", "stress": ..., "scale": ... },
  "portfolio":{ /* from output/portfolio.json */ },
  "signals":  [ /* one per row with features */ ],
  "veto_history_30d": { /* per-strategy counts */ },
  "mastermind_rec": { /* last Friday's sizing recommendation */ }
}
```

Size-bounded (~290 KB for 576 signals) — stays well under TradeJohn's
200K context.

### 2.5 `trade`

`src/execution/trade_agent_llm.py` invokes TradeJohn (Claude
`sonnet-4-6`, $0.70 per-call budget, 15 iteration cap) via
`src/agent/run-subagent-cli.js`. TradeJohn reads the structured handoff,
runs Kelly sizing, and emits a markdown memo with a fenced
` ```tradejohn_orders ` JSON block. The block is parsed and written to
`handoff:{run_date}:sized`.

Budget gate runs BEFORE this step. If `budget:mode == RED` or token
budget is exhausted, the orchestrator pauses here and `checkPipelineResume()`
picks up 30-minutely once budget recovers.

### 2.6 `alpaca`

`src/execution/alpaca_executor.py` reads the sized handoff, submits
bracket orders (market + take-profit + stop-loss) to Alpaca Paper via
their REST API. Size caps: 5% NAV per order, 25% NAV new notional
per day. Idempotent via `alpaca_submissions (run_date, strategy_id,
ticker)` unique constraint.

Time-in-force is `day` during RTH (09:30–16:00 ET), `opg` otherwise.

### 2.7 `report`

`src/execution/send_report.py` posts two concise messages:
- **`#trade-signals`** — greenlist table (ticker, strategy, dir, entry,
  size%, EV%, p(T1)).
- **`#trade-reports`** — veto digest grouped by reason with sample tickers.

---

## 3. Weekly MastermindJohn (Opus 4.7, 1M context)

Two modes under `src/agent/curators/run_mastermind.js`:

### 3.1 `--mode strategy-stack` (Fri 20:00 ET)

Timer: `openclaw-mastermind-weekly.timer`. Flow:

1. Read `live + monitoring` strategies from `src/strategies/manifest.json`.
2. Load per-strategy rows from `strategy_stats` (view, migration 042),
   `daily_signal_summary` (migration 040), `alpaca_submissions`
   (migration 043), `position_recommendations` (migration 022), and
   `veto_log`.
3. One Opus 4.7 call (subagent type `mastermind`) synthesizes a
   comprehensive-but-concise memo with per-strategy notes, cross-strategy
   correlation, and a fenced ` ```sizing_recommendations ` JSON block.
4. Post memo to `#strategy-memos`, sizing JSON to
   `#position-recommendations`.
5. Persist to `mastermind_weekly_reports` (migration 047).

`trade_handoff_builder.py::load_mastermind_rec()` reads the latest row
each daily cycle so TradeJohn sees the week's sizing guidance.

### 3.2 `--mode corpus` (Sat 10:00 ET)

Timer: `openclaw-mastermind-corpus.timer`. Legacy CorpusCurator flow —
arXiv + OpenAlex discovery, then a full pass over `research_corpus`
with batch size 100. Promotes confidence ≥ 0.75 to
`research_candidates`, hard cap 600 promotions per run.

---

## 4. Data-column queue (user-driven)

The operator approves a staging strategy on the dashboard. The approval
worker (`src/agent/approvals/staging_approver.js`) reads the strategy's
`required_columns` manifest, validates every column against
`schema_registry.json`, and inserts `data_ingestion_queue` rows with
`backfill_from`/`backfill_to` set from each column's `lookback_days`.

On the next 10:00 AM cycle, `queue_drain` backfills historical data for
the new columns, then the `collect` step picks them up in its normal
coverage scan. All subsequent daily cycles include the new column.

When an active strategy is unstacked (transitioned to `deprecated` or
`archived`), `src/strategies/lifecycle.py::_enqueue_orphan_columns()`
inserts auto-`APPROVED` rows into `data_deprecation_queue` for columns
no remaining `live|paper|monitoring` strategy consumes. Next
`queue_drain` removes them from the live collection set.

Symmetry: only unstacking triggers removal; approvals trigger additions.

---

## 5. Discord channel map

| Channel | Publisher | Content |
|---|---|---|
| `#pipeline-feed` | pipeline_orchestrator.py | ▶️/✅/❌ phase boundaries |
| `#data-alerts` | freshness.js, queue_drain.py, collector.js | Staleness alerts, backfill progress, API errors |
| `#trade-signals` | send_report.py | Daily greenlist (one post) |
| `#trade-reports` | send_report.py | Daily veto digest |
| `#strategy-memos` | strategy_stack.js (Fri only) | Weekly stack memo |
| `#position-recommendations` | strategy_stack.js (Fri only) | Sizing deltas JSON |
| `#botjohn-log` | bot.js | System events |
| `#research-feed` | (manual / ad-hoc) | Legacy — kept for ad-hoc research subagent runs |

Per-strategy memos (`post_memos.py` path) were removed in Phase 2 and no
longer post anywhere.

---

## 6. Invariants

1. **All LLM work happens inside `trade`.** Every other step is
   deterministic. If a new step needs LLM, it must be explicitly
   budget-gated.
2. **Strategies never import from `agent/` or `channels/`.** Must be
   runnable in a Python REPL with only a DataFrame + regime dict.
3. **`generate_signals` is deterministic.** Any RNG must be seeded.
4. **`manifest.json` is the lifecycle truth** for live/paper/monitoring/
   deprecated/archived. `strategy_registry.status` uses older labels —
   don't confuse the two.
5. **`pipeline:resume_checkpoint` takes precedence over `budget:mode ==
   RED`.** Resume only when mode ≠ RED.
6. **`_IMPL_MAP` aliases are historical.** Never rename a strategy id —
   `signals` rows reference them. Only add new ids.
7. **Column removal is unstack-driven only.** Never auto-enqueue
   `data_deprecation_queue` from other code paths; the reaper's weekly
   pass is the only other acceptable source (for truly orphaned
   columns no strategy ever used).

---

## 7. Critical file map

| File | Role |
|---|---|
| `src/engine/cron-schedule.js` | 10:00 AM cron + token-reset + weekly maintenance |
| `src/execution/pipeline_orchestrator.py` | 7-step supervisor, checkpointing, Discord phase posts |
| `src/execution/engine.py` | Signal execution, confluence detection, P&L updates |
| `src/execution/trade_handoff_builder.py` | Features → structured handoff (replaces research_report) |
| `src/execution/trade_agent_llm.py` | TradeJohn invocation + sized handoff writer |
| `src/execution/alpaca_executor.py` | Bracket-order submission |
| `src/execution/send_report.py` | Daily Discord greenlist + veto digest |
| `src/execution/handoff.py` | Redis+filesystem handoff primitives |
| `src/pipeline/collector.js` | Master parquet collector (universe coverage) |
| `src/pipeline/run_collector_once.js` | CLI wrapper — one collection cycle |
| `src/pipeline/queue_drain.py` | Column backfill + deprecation drainer |
| `src/pipeline/backfillers/*.py` | Per-provider history loaders |
| `src/pipeline/freshness.js` | Staleness detector → `#data-alerts` |
| `src/strategies/base.py` | `BaseStrategy` + `Signal` (with `features` field) |
| `src/strategies/registry.py` | `_IMPL_MAP` + approved-strategy loader |
| `src/strategies/lifecycle.py` | State machine + unstack-triggered column queue |
| `src/strategies/manifest.json` | Canonical lifecycle states |
| `src/agent/curators/mastermind.js` | MastermindJohn corpus mode |
| `src/agent/curators/strategy_stack.js` | MastermindJohn strategy-stack mode |
| `src/agent/curators/run_mastermind.js` | CLI (`--mode {corpus,strategy-stack}`) |
| `src/agent/prompts/subagents/mastermind.md` | Opus prompt (shared across modes) |
| `docs/mastermind-corpus.{service,timer}` | Sat 10:00 ET systemd |
| `docs/mastermind-weekly.{service,timer}` | Fri 20:00 ET systemd |

---

*If this file disagrees with what's running on the VPS, this file is wrong
and should be corrected before the code is touched.*
