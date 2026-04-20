# CLAUDE.md — FundJohn / OpenClaw System

This project contains all system optimizations for FundJohn, a bot-network quantitative hedge fund built on OpenClaw using Claude Code agents.

## System Overview
Autonomous quant PM system + hardcoded data pipeline:
- **BotJohn** (claude-opus-4-6): Orchestrator and portfolio manager
- **DataPipeline** (hardcoded, src/execution/runner.js): Strategy execution, data collection, memo dispatch
- **ResearchJohn** (claude-sonnet-4-6): Strategy memo synthesis and research reporting
- **TradeJohn** (claude-sonnet-4-6): Signal generation and position sizing
- **PaperHunter** (claude-haiku-4-5): Per-paper extraction + 4 rejection gates
- **StrategyCoder** (claude-sonnet-4-6): On-demand strategy implementation
- **CorpusCurator** (claude-opus-4-7, 1M ctx): Weekly corpus-level paper curator — rates the full `research_corpus` in batched calls and promotes high-confidence picks to `research_candidates`. Runs Saturday 10:00 ET via `openclaw-curator.service`/`.timer`.

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

## Key Paths (VPS: /root/openclaw/)
- `src/strategies/lifecycle.py` — strategy state machine
- `src/strategies/manifest.json` — strategy registry
- `src/strategies/implementations/` — strategy Python files
- `src/agent/main.js` — agent orchestrator entry point
- `src/agent/prompts/subagents/` — agent prompt files
- `src/agent/curators/corpus_curator.js` — Opus Corpus Curator orchestrator (Phase 1–5)
- `src/agent/curators/run_curator.js` — CLI entry for the Saturday timer
- `src/agent/research/gate-decisions.js` — structured `paper_gate_decisions` emitter
- `src/ingestion/arxiv_discovery.py` — broad arXiv q-fin harvest into `research_corpus`
- `src/ingestion/openalex_discovery.py` — SSRN/NBER/JFE/RFS/JF/JFQA/QF + author watchlist + citation graph
- `src/database/migrations/032..038_*.sql` — corpus + calibration + ROI schema
- `docs/curator.service` · `docs/curator.timer` — systemd units (installed at `/etc/systemd/system/`)
- `agents/` — agent identity and soul files
- `data/` — master parquet datasets
- `johnbot/` — Discord bot
- `.env` — secrets and config

## Deployment Workflow
Changes flow: local edit → git commit + push → VPS `git pull`
For large files: python3 base64 decode command → paste on VPS → git add/commit/push
