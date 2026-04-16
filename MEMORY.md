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
States: candidate → paper → live → monitoring → deprecated → archived

### Active Strategies (as of 2026-04-15)
- **Live (6):** S5_max_pain, S9_dual_momentum, S10_quality_value, S12_insider, S15_iv_rv_arb, S_custom_jt_momentum_12mo
- **Paper (3):** S23_regime_momentum, S24_52wk_high_proximity, S25_dual_momentum_v2
- **Deprecated (1):** S_custom_momentum_trend_v1 (audit F3 — pending archive)

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
