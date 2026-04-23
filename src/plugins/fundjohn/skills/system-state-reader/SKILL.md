# Skill: fundjohn:system-state-reader
**Trigger**: `/system-state` or `/state` or `/status-reader`

## Purpose
Return a canonical one-screen snapshot of system state. Used by BotJohn
when the operator asks `@FundJohn status`, when BotJohn needs grounding
for any ad-hoc diagnostic, and when Discord `#ops` heartbeat messages
need a consistent shape. Reads only; never mutates.

This skill replaces the scattershot "BotJohn re-derives state from N
separate MCP calls" anti-pattern. One skill, one shape.

## Data Sources (all local, no network)

| Field            | Source                                         |
|------------------|------------------------------------------------|
| `budget.mode`    | Redis `budget:mode`                            |
| `budget.daily`   | Redis `budget:daily_usd`                       |
| `budget.monthly` | Redis `budget:monthly_usd`                     |
| `budget.cap`     | `config/budget.json` → `monthly_cap_usd`       |
| `regime.state`   | `workspaces/default/regime.json` → state       |
| `regime.stress`  | `workspaces/default/regime.json` → stress      |
| `regime.scale`   | `workspaces/default/regime.json` → position_scale |
| `pipeline.lock`  | Redis `pipeline:running:{YYYY-MM-DD}`          |
| `pipeline.resume`| Redis `pipeline:resume_checkpoint`             |
| `signals.today`  | Postgres `execution_signals` WHERE signal_date = today |
| `orders.today`   | Postgres `alpaca_submissions` WHERE run_date = today |
| `veto.digest`    | Postgres `veto_log` last 7 days, grouped       |

## Output Schema (MUST match exactly)

```json
{
  "as_of": "2026-04-23T14:32:00-04:00",
  "budget": {
    "mode":    "GREEN",
    "daily":   3.42,
    "monthly": 127.18,
    "cap":     400.00,
    "monthly_pct": 31.8
  },
  "regime": {
    "state":  "LOW_VOL",
    "stress": 18.2,
    "scale":  1.00
  },
  "pipeline": {
    "lock_active":       true,
    "lock_run_date":     "2026-04-23",
    "resume_checkpoint": null,
    "last_completed":    "2026-04-22"
  },
  "signals_today": {
    "total":       63,
    "green_count": 14,
    "vetoed_count":49
  },
  "orders_today": {
    "submitted": 12,
    "filled":    11,
    "rejected":  1
  },
  "veto_digest_7d": [
    {"strategy_id": "S9_dual_momentum", "reason": "negative_ev", "n": 8}
  ]
}
```

## Usage

```
/system-state                 → emit the JSON snapshot above
/system-state --brief         → emit a single-sentence status line only
/system-state --no-redis      → skip Redis reads (if Redis is down, use SQL fallbacks)
```

## Brief-mode format

```
🟢 GREEN · LOW_VOL · pipeline IDLE · 14 green / 49 vetoed today · budget $127/$400 (31.8%)
```

Emoji mapping: GREEN=🟢, YELLOW=🟡, RED=🔴.

## Hard Rules

- Never write. This skill is a pure reader.
- Never invoke an MCP provider — all state is local (Redis + Postgres + JSON).
- If any source is unavailable, set that field to `null` and include a
  `degraded: [{"field": "regime.state", "reason": "regime.json missing"}]`
  list at the top level. Do not refuse to answer.
- Times ISO 8601 with timezone offset. Percentages to 1 decimal.
