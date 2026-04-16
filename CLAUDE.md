# CLAUDE.md — FundJohn / OpenClaw System

This project contains all system optimizations for FundJohn, a bot-network quantitative hedge fund built on OpenClaw using Claude Code agents.

## System Overview
3-agent autonomous quant PM system + hardcoded data pipeline:
- **BotJohn** (claude-opus-4-6): Orchestrator and portfolio manager
- **DataPipeline** (hardcoded, src/execution/runner.js): Strategy execution, data collection, memo dispatch
- **ResearchJohn** (claude-sonnet-4-6): Strategy memo synthesis and research reporting
- **TradeJohn** (claude-sonnet-4-6): Signal generation and position sizing

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
- `agents/` — agent identity and soul files
- `data/` — master parquet datasets
- `johnbot/` — Discord bot
- `.env` — secrets and config

## Deployment Workflow
Changes flow: local edit → git commit + push → VPS `git pull`
For large files: python3 base64 decode command → paste on VPS → git add/commit/push
