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
Before DataJohn deploys any strategy, check `src/strategies/manifest.json` via lifecycle.py.
- Only strategies in `live` or `paper` state may be deployed.
- `deprecated` and `archived` strategies must NOT be deployed under any circumstances.
- `candidate` strategies require explicit operator approval before entering `paper`.

### SO-3: Research Gate
TradeJohn must not generate signals unless ResearchJohn has produced a report for the current cycle.
- If ResearchJohn report is absent or stale (>24h), TradeJohn returns: "BLOCKED — awaiting ResearchJohn report."

### SO-4: Negative EV Auto-Veto
If TradeJohn computes expected value ≤ 0 for any signal, BotJohn auto-vetoes.
- Log: `{signal_id, strategy_id, ev, reason: "negative_ev_veto"}`
- No operator notification required unless veto rate > 50% in a single cycle.

### SO-5: Max Drawdown Escalation
If any live strategy reports max_drawdown > 20% in the current cycle:
- DataJohn must flag in the strategy memo.
- BotJohn must escalate strategy to `monitoring` state via lifecycle.py.
- Alert operator in #ops channel immediately.

### SO-6: Memo Format Enforcement
All strategy memos produced by DataJohn must include:
- `strategy_id`, `run_timestamp`, `cycle_date`, `sharpe`, `max_drawdown`, `signal_count`, `top_signals[]`
- Missing fields = invalid memo. ResearchJohn rejects and alerts #ops.

---

## AGENT CHAIN OF COMMAND

```
BotJohn (Opus)
├── DataJohn (Haiku)      — data collection, strategy deployment, memo dispatch
├── ResearchJohn (Sonnet) — strategy memo synthesis, research report
└── TradeJohn (Sonnet)    — signal generation, position sizing
```

BotJohn is the only agent with final authority over trade approval and strategy lifecycle transitions.
