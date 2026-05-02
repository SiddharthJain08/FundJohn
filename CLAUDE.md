# CLAUDE.md — FundJohn / OpenClaw System

This project contains all system optimizations for FundJohn, a bot-network quantitative hedge fund built on OpenClaw using Claude Code agents.

## Core invariant — NEVER DELETE FROM THE MASTER DATABASE
The master parquets and Postgres tables under `data/master/` are append-only.
Columns may be ADDED at any time. Tickers may be ADDED at any time. Date
ranges may only grow. **No code path is allowed to drop columns, drop
tickers, truncate the date axis, deprecate columns, or delete rows from
prices.parquet, options_eod.parquet, financials.parquet, macro.parquet,
insider.parquet, earnings.parquet, prices_30m.parquet, or
historical_regimes.parquet.** Same rule applies to the canonical
Postgres tables (`execution_signals`, `signal_pnl`, `alpaca_submissions`,
`data_coverage`, `data_columns`). The system's job is to grow the data
forever and let strategies opt into whichever subset they need; it is
NOT to optimize storage by pruning. Any future "deprecation" must be a
flag (`active=false`) on a metadata row, never a `DELETE`.

## System Overview
Autonomous quant PM system + hardcoded data pipeline:
- **BotJohn** (claude-opus-4-6): Orchestrator and portfolio manager
- **DataPipeline** (hardcoded, src/execution/pipeline_orchestrator.py): 6-step 10am daily cycle — collect → signals → handoff → trade → alpaca → report. (queue_drain removed 2026-04-28; fused-staging-approval handles inline column backfills.)
- **TradeJohn** (claude-sonnet-4-6): Signal selection + Kelly sizing. Reads the structured handoff from `trade_handoff_builder.py`; emits sized bracket orders. Single LLM step in the cycle.
- **PaperHunter** (claude-haiku-4-5): Per-paper extraction + 4 rejection gates
- **StrategyCoder** (claude-sonnet-4-6): On-demand strategy implementation
- **MastermindJohn** (claude-opus-4-7, 1M ctx): Opus orchestrator with four weekly modes.
  - `mode=corpus` (Sat 10:00 ET via `openclaw-mastermind-corpus.service`/`.timer`) — rates the full `research_corpus` in batched calls and promotes high-confidence picks to `research_candidates`.
  - `mode=comprehensive-review` (Sat 18:00 ET via `openclaw-strategy-review.service`/`.timer`) — deep per-strategy lifetime review: every closed trade, counterfactual tuning of size / stop / target / max-hold for greater profitability. Writes to `strategy_memos`; posts each memo to `#strategy-memos`.
  - `mode=position-recs` (Sat 19:00 ET via `openclaw-position-recs.service`/`.timer`) — reads the latest `strategy_memos`, distils them into exact per-strategy sizing + bracket deltas, writes to `strategy_sizing_recommendations`, posts consolidated table to `#position-recommendations`. Feeds TradeJohn's Monday handoff via `trade_handoff_builder.py`.
  - `mode=paper-expansion` (Sun 08:00 ET via `openclaw-paper-expansion.service`/`.timer`) — Opus + WebSearch/WebFetch steers an open-ended source-discovery sweep beyond arXiv/OpenAlex (journals, working-paper series, blogs, conference proceedings), scrapes, dedupes, imports into `research_corpus`. Logs to `paper_source_expansions`.
  - Was `CorpusCurator` prior to 2026-04-22 Phase 3; legacy `corpus-curator` subagent type still resolves to the same prompt for backward compat. Legacy `strategy-stack` mode + `mastermind_weekly_reports` table were deleted 2026-04-24 — they were pipeline-building scaffolding, not production features.

## Context Retention
Retain all context and memory of:
- File locations on the VPS (/root/openclaw/)
- Current strategy lifecycle states (from manifest.json)
- Agent architecture and responsibilities
- Changes made and bottlenecks fixed

At every step maintain a complete understanding of:
- Which strategies are live/paper/deprecated
- What data collections are active
- Current portfolio state
- Any pending lifecycle transitions

## LangGraph Orchestration (added 2026-04-22)
The cycle and paper-hunt flows run through LangGraph.js:
- `src/agent/graph.js` — daily cycle StateGraph (datajohn → tradejohn → HITL → botjohn); PostgresSaver checkpointer in `langgraph` schema; `interruptBefore: ['botjohn']` for operator approval; conditional edge skips botjohn if tradejohn produced zero signals. (ResearchJohn retired 2026-05-02 — mastermind handles research via saturday_brain.js + comprehensive_review.js, not this graph.)
- `src/agent/graphs/paperhunter.js` — Send-based parallel fan-out for paper extraction.
- `src/agent/graphs/index.js` — graph registry. Add new flows here.
- `src/agent/traceBus.js` — in-memory event ring buffer fanning out to dashboard SSE.
- `bin/run-graph.js` — CLI runner: `node bin/run-graph.js list | cycle '<json>' | cycle:resume '<json>' | cycle:state <threadId>`.
- Dashboard: `src/channels/dashboard/server.js` on 127.0.0.1:7870 (systemd: `fundjohn-dashboard.service`). SSH-tunnel to view. Surfaces bots, subagents, analyses, verdicts, trades, checkpoints, workspaces, graph runs + live traces, HITL approve/veto buttons.
- Smoke tests: `node test/graph-smoke.js` (cycle HITL + veto), `node test/paperhunter-smoke.js` (fan-out parallelism).
- Set `LANGSMITH_API_KEY` in `.env` to auto-enable LangSmith tracing (project=`fundjohn`).

## Key Paths (VPS: /root/openclaw/)
- `src/strategies/lifecycle.py` — strategy state machine
- `src/strategies/manifest.json` — strategy registry
- `src/strategies/implementations/` — strategy Python files
- `src/agent/main.js` — agent orchestrator entry point
- `src/agent/prompts/subagents/` — agent prompt files
- `src/agent/curators/mastermind.js` — MastermindJohn corpus-mode orchestrator
- `src/agent/curators/comprehensive_review.js` — Saturday per-strategy memos
- `src/agent/curators/position_recommender.js` — Saturday sizing recs from memos
- `src/agent/curators/paper_expansion_ingestor.js` — Sunday Opus-steered paper hunt
- `src/agent/curators/run_mastermind.js` — CLI entry (`--mode {corpus|comprehensive-review|position-recs|paper-expansion}`)
- `src/agent/research/gate-decisions.js` — structured `paper_gate_decisions` emitter
- `src/ingestion/arxiv_discovery.py` — broad arXiv q-fin harvest into `research_corpus`
- `src/ingestion/openalex_discovery.py` — SSRN/NBER/JFE/RFS/JF/JFQA/QF + author watchlist + citation graph
- `src/database/migrations/032..038_*.sql` — corpus + calibration + ROI schema
- `docs/mastermind-corpus.{service,timer}` — Saturday 10:00 ET corpus rater (installed at `/etc/systemd/system/openclaw-mastermind-corpus.*`)
- `docs/strategy-review.{service,timer}` — Saturday 18:00 ET comprehensive review
- `docs/position-recs.{service,timer}` — Saturday 19:00 ET sizing recs
- `docs/paper-expansion.{service,timer}` — Sunday 08:00 ET paper expansion ingestion
- `src/agent/run_maintenance.js` — Daily 12:00 ET (Mon-Fri) BotJohn maintenance driver: doctor + digest + investigate + fix + post to #general
- `docs/botjohn-maintenance.{service,timer}` — Mon-Fri 12:00 ET maintenance run (installed at `/etc/systemd/system/openclaw-botjohn-maintenance.*`)
- `docs/botjohn-saturday-maintenance.{service,timer}` — Sat 16:00 ET research-pipeline audit + surgical recovery (installed at `/etc/systemd/system/openclaw-botjohn-saturday-maintenance.*`)
- `docs/botjohn-saturday-verify.{service,timer}` — Sun 12:00 ET read-only verification of any Saturday recovery (installed at `/etc/systemd/system/openclaw-botjohn-saturday-verify.*`)
- `src/agent/run_maintenance.js` — single wrapper for all three maintenance modes; dispatches on `--mode {daily,saturday,saturday-verify}` (default `daily`)
- `agents/` — agent identity and soul files
- `data/` — master parquet datasets
- `johnbot/` — Discord bot
- `.env` — secrets and config

## Deployment Workflow
Changes flow: local edit → git commit + push → VPS `git pull`
For large files: python3 base64 decode command → paste on VPS → git add/commit/push
