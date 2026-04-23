# CLAUDE.md — FundJohn / OpenClaw System

This project contains all system optimizations for FundJohn, a bot-network quantitative hedge fund built on OpenClaw using Claude Code agents.

## System Overview
Autonomous quant PM system + hardcoded data pipeline:
- **BotJohn** (claude-opus-4-6): Orchestrator and portfolio manager
- **DataPipeline** (hardcoded, src/execution/pipeline_orchestrator.py): 7-step 10am daily cycle — queue_drain → collect → signals → handoff → trade → alpaca → report.
- **TradeJohn** (claude-sonnet-4-6): Signal selection + Kelly sizing. Reads the structured handoff from `trade_handoff_builder.py`; emits sized bracket orders. Single LLM step in the cycle.
- **PaperHunter** (claude-haiku-4-5): Per-paper extraction + 4 rejection gates
- **StrategyCoder** (claude-sonnet-4-6): On-demand strategy implementation
- **MastermindJohn** (claude-opus-4-7, 1M ctx): Opus orchestrator with two modes.
  - `mode=corpus` (Sat 10:00 ET via `openclaw-mastermind-corpus.service`/`.timer`) — rates the full `research_corpus` in batched calls and promotes high-confidence picks to `research_candidates`.
  - `mode=strategy-stack` (Fri 20:00 ET via `openclaw-mastermind-weekly.service`/`.timer`) — analyses the live+monitoring strategy stack over each strategy's lifetime; posts a memo to `#strategy-memos` and structured sizing deltas to `#position-recommendations`. Latest row in `mastermind_weekly_reports` feeds TradeJohn's Monday handoff via `trade_handoff_builder.py`.
  - Was `CorpusCurator` prior to 2026-04-22 Phase 3. The deprecated `corpus-curator` subagent alias was removed on 2026-04-23 — use `type: 'mastermind'` everywhere.

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
- `src/agent/graph.js` — daily cycle StateGraph (datajohn → researchjohn → tradejohn → HITL → botjohn); PostgresSaver checkpointer in `langgraph` schema; `interruptBefore: ['botjohn']` for operator approval; conditional edge skips botjohn if tradejohn produced zero signals.
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
- `src/agent/curators/strategy_stack.js` — MastermindJohn strategy-stack mode (weekly)
- `src/agent/curators/run_mastermind.js` — CLI entry (`--mode {corpus|strategy-stack}`)
- `src/agent/research/gate-decisions.js` — structured `paper_gate_decisions` emitter
- `src/ingestion/arxiv_discovery.py` — broad arXiv q-fin harvest into `research_corpus`
- `src/ingestion/openalex_discovery.py` — SSRN/NBER/JFE/RFS/JF/JFQA/QF + author watchlist + citation graph
- `src/database/migrations/032..038_*.sql` — corpus + calibration + ROI schema
- `docs/mastermind-corpus.{service,timer}` · `docs/mastermind-weekly.{service,timer}` — systemd units (installed at `/etc/systemd/system/openclaw-mastermind-corpus.*` and `openclaw-mastermind-weekly.*`)
- `agents/` — agent identity and soul files
- `data/` — master parquet datasets
- `johnbot/` — Discord bot
- `.env` — secrets and config

## Deployment Workflow
Changes flow: local edit → git commit + push → VPS `git pull`
For large files: python3 base64 decode command → paste on VPS → git add/commit/push
