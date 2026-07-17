# MEMORY.md — What Should BotJohn Always Know?

## Preferences
- Python for strategy implementations and data pipelines
- Node.js for bot infrastructure (Discord, orchestrator)
- Markdown for all report and memo output
- Tables over prose for financial data
- Always rank, never just list

## System Architecture

### FundJohn / OpenClaw (Active — Primary)
A 4-agent autonomous quant hedge fund system running on Claude Code.

```
Operator (Discord)
       │
    BotJohn  ◄── claude-opus-4-6  (orchestrator, portfolio manager)
    /  |  \
   /   |   \
ResearchJohn  TradeJohn
(Sonnet)      (Sonnet)
DataPipeline: hardcoded
```

### Agent Responsibilities
| Agent | Model | Job |
|---|---|---|
| BotJohn | claude-opus-4-6 | Orchestrate, approve trades, manage portfolio, Discord interface |
| DataPipeline | hardcoded | Collect market data, deploy strategies, send strategy memos |
| ResearchJohn | claude-sonnet-4-6 | Read strategy memos, produce research report |
| TradeJohn | claude-sonnet-4-6 | Signal generation, position sizing |

### Strategy Lifecycle
Managed by `src/strategies/lifecycle.py` + `src/strategies/manifest.json`
States: `staging → candidate → live → monitoring → deprecated → archived`. `paper` is frozen legacy (no outbound transitions except safety-valve `paper → archived`) since 2026-04-27 fused-approval rewrite.

### Active Strategies
Drifts fast — don't pin a count or per-strategy list here. Read `src/strategies/manifest.json` for current state. Authoritative; dual-written to Postgres `strategy_registry`.

### Key File Paths (VPS: /root/openclaw/)
- Strategy lifecycle: `src/strategies/lifecycle.py`
- Strategy registry: `src/strategies/manifest.json`
- Strategy implementations: `src/strategies/implementations/`
- Data store: `data/*.parquet` (prices, financials, options_eod, macro, insider)
- Agent orchestrator: `src/agent/main.js`
- Discord bot: `johnbot/index.js`
- Configs: `.env`, `src/agent/config/`

## Decisions Already Made — Do Not Revisit
1. **Architecture**: 3-agent system (BotJohn + ResearchJohn + TradeJohn) + hardcoded data pipeline. No sub-swarms.
2. **Models**: BotJohn=Opus, ResearchJohn=Sonnet, TradeJohn=Sonnet. DataPipeline=hardcoded (runner.js).
3. **Strategy lifecycle**: lifecycle.py is the single source of truth. No manual state edits in manifest.json.
4. **Data pipeline**: Hardcoded runner.js (runDailyClose). Append-only parquet files. No LLM agent.
5. **Promotion gate**: paper→live requires Sharpe ≥ 0.5 AND max_drawdown ≤ 20%.
