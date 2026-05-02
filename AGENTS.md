# OpenClaw — Agent Standing Orders

Deterministic behavioral rules embedded in all agent contexts.
No API calls, no token spend. Enforced at the start of every operation.

---

## STANDING ORDERS

### SO-1: Budget Mode Gate
Check Redis key `budget:mode` before any non-essential operation.
- **GREEN**: proceed normally
- **YELLOW**: reject non-essential operations. Respond: "Budget YELLOW ($X/day) — only essential ops permitted."
- **RED**: reject all operations except operator-manual triggers. Respond: "Budget RED — operator must approve."

### SO-2: Lifecycle State Gate
Before the data pipeline deploys any strategy, `src/strategies/manifest.json` is checked via lifecycle.py.
- Only strategies in `live` or `paper` state may be deployed.
- `deprecated` and `archived` strategies must NOT be deployed under any circumstances.
- `candidate` strategies require explicit operator approval before entering `paper`.

### SO-3: Research Gate
TradeJohn reads the structured handoff from `trade_handoff_builder.py` directly.
- (ResearchJohn was retired 2026-05-02. The daily cycle is now
  `datajohn → tradejohn → botjohn` with no separate research-synthesis step.
  MastermindJohn handles weekly research via saturday_brain.js +
  comprehensive_review.js, posting to #research-feed and #strategy-memos.)

### SO-4: Negative EV Auto-Veto
If TradeJohn computes expected value ≤ 0 for any signal, BotJohn auto-vetoes.
- Log: `{signal_id, strategy_id, ev, reason: "negative_ev_veto"}`
- No operator notification required unless veto rate > 50% in a single cycle.

### SO-5: Max Drawdown Escalation
If any live strategy reports max_drawdown > 20% in the current cycle:
- The data pipeline must flag in the strategy memo.
- BotJohn must escalate strategy to `monitoring` state via lifecycle.py.
- Alert operator in #ops channel immediately.

### SO-6: Memo Format Enforcement
All strategy memos produced by the data pipeline must include:
- `strategy_id`, `run_timestamp`, `cycle_date`, `sharpe`, `max_drawdown`, `signal_count`, `top_signals[]`
- Missing fields = invalid memo. The data pipeline rejects and alerts #ops.

### SO-7: Token Economy (Cache-First)
Only pay for novel work. Every token spent must either generate new knowledge or size a position.
- Check Redis handoff layer (`handoff:{date}:{stage}`) before re-running any upstream computation.
- `cache_read_input_tokens` tracked separately in `subagent_costs`. Cache hit rate < 30% over 7 days triggers prompt structure review.
- PaperHunter hard cap: $0.15 per invocation. Budget exceeded = immediate termination.

---

## AGENT CHAIN OF COMMAND

```
BotJohn (Opus) — final authority
├── DataPipeline (hardcoded, daily cron 4:20 PM ET)
│   ├── post_memos.py         — engine run + SO-6 memo generation
│   ├── research_report.py    — signal enrichment (HV/beta/EV, pure Python, no LLM)
│   ├── TradeJohn (Sonnet)    — daily: sizing + signal generation from memos
│   └── portfolio_report.py   — portfolio metrics
└── MastermindJohn (Opus 4.7, weekly Saturday)
    ├── saturday_brain.js              — paper sweep + corpus rating + Tier-A code+backtest
    ├── PaperHunter (Sonnet 4.6, parallel) — alpha paper extraction
    └── StrategyCoder (Sonnet 4.6)     — implementation + registry + manifest wiring
```

BotJohn is the only agent with final authority over trade approval and strategy lifecycle transitions.

**Daily pipeline:** `post_memos.py → research_report.py → trade_agent_llm.py → portfolio_report.py`
**Research pipeline:** operator-triggered, pauseable, budget-capped at $2.00/session by default.
