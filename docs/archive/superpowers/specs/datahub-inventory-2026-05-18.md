# DataHub Phase 2B — Redis Caller Inventory

**Date:** 2026-05-18
**Branch:** feat/phase-2b-datahub
**Method:** `grep -rn -E "(r|redis|client|_r|_client|_redis)\.(publish|setex|set|hset|hmset|expire)\s*\(" src/`
**Total call sites:** 35 across 13 files (8 JS, 5 Python)

This file freezes the pre-facade state. Each caller listed below is a
candidate for an incremental migration to `src.database.datahub.DataHub`
in a follow-up commit. The facade ships ALONGSIDE these callers — none
is deleted in Phase 2B. Per-caller migration is the topic of B2.6 and
later phases.

## Grouped by Purpose

### Status / presence (5 sites)
| File | Line | Key | Op |
|---|---|---|---|
| src/database/redis.js | 98 | `subagent:{id}` | setex(3600) |
| src/channels/discord/agent-personas.js | 156 | `agent_persona:{id}` | setex |
| src/channels/discord/agent-personas.js | 169 | `agent:presence` | publish |
| src/execution/pipeline_orchestrator.py | 146 | `agent_status:{id}` | setex(300) |
| src/execution/pipeline_orchestrator.py | 149 | `agent:presence` | publish |

Note the schema drift: three different status conventions
(`subagent:`, `agent_persona:`, `agent_status:`) for the same domain.
Topic-registry canonical form is `agent:status:{agent_id}`. Existing
non-conforming keys stay in place until per-caller migration.

### Steering / control (4 sites)
| File | Line | Key | Op |
|---|---|---|---|
| src/agent/research/research-orchestrator.js | 116 | `STOP_AFTER_KEY` | set EX 86400 |
| src/agent/research/research-orchestrator.js | 202, 828 | `PAUSE_KEY` | set EX 86400 |
| src/agent/research/research-orchestrator.js | 832 | `STOP_AFTER_KEY` | set EX 86400 |

Also `pushSteering` / `drainSteering` in `src/database/redis.js` use
`rpush`/`lpop` on `steering:{threadId}` — NOT in scope for DataHub
(list semantics, not pub/sub or key/TTL).

### Rate-limit (10 sites)
| File | Line | Key | Op |
|---|---|---|---|
| src/database/redis.js | 46–67 | `rate_limit:{provider}` | set (refill every 60s) |
| src/database/redis.js | 147–148 | `ratelimit:{provider}:reset_at`/`:remaining` | set EX |
| src/pipeline/collector.js | 256–257 | `rate_backoff:{provider}` | set EX |

Note schema drift again: `rate_limit:` vs `ratelimit:` vs `rate_backoff:`.
DataHub rate-limit facility (B2.4) is producer-side soft fail; the
existing provider-side buckets above are separate and stay as-is.

### Cache (6 sites)
| File | Line | Key | Op |
|---|---|---|---|
| src/database/redis.js | 120 | `cache:{key}` | setex(300) |
| src/agent/tools/registry.js | 92, 151 | `cache:{key}` (Python tool gen) | setex |
| src/pipeline/data_cache.py | 100 | (configurable key) | setex |
| src/ingestion/massive_ws.py | 260 | `massive:flow:{underlying}` | set EX |
| src/channels/discord/agent-personas.js | 156 | `agent_persona:{id}` | setex |

### Lock / checkpoint (5 sites)
| File | Line | Key | Op |
|---|---|---|---|
| src/execution/pipeline_orchestrator.py | 165, 700 | `{LOCK_KEY}:{date}` | set NX EX |
| src/execution/pipeline_orchestrator.py | 187 | `CHECKPOINT_KEY` | set EX 86400 |
| src/execution/pipeline_orchestrator.py | 212 | `{COMPLETED_KEY}:{run_date}` | set EX |
| src/pipeline/data_cache.py | 130 | lock_key | set NX EX |
| src/agent/middleware/pipeline-activity.js | 26 | activity key | set EX 1800 |

### Handoff (1 site)
| File | Line | Key | Op |
|---|---|---|---|
| src/execution/handoff.py | 54 | `handoff:{date}:{stage}` | setex(86400) |

**This is the demonstrator caller for B2.5.** See B2.5 commit message.

### Token budget (1 site)
| File | Line | Op |
|---|---|---|
| src/agent/middleware/token-budget.js | 58 | expire (TTL extension) |

### Run state (3 sites)
| File | Line | Key | Op |
|---|---|---|---|
| src/execution/runner.js | 80, 84, 118 | strategy run state | set [EX] |

### Alerts publish (1 site)
| File | Line | Topic | Op |
|---|---|---|---|
| src/pipeline/collector.js | 258 | `data:alerts` | publish |

## Fragility list (DO NOT touch in this phase)
Per spec hard constraint 5:
- `src/execution/regime_blended_sizer_live.py` (live sizer; not in inventory above — uses Redis indirectly via `regime_blended_sizer.py`)
- `_steering` keys consumed by middleware (`src/agent/middleware/steering.js`)
- SLAVEOF iptables defense rule (infrastructure, not code)

## Branch-point decision
Per spec line 760, choosing **(a) facade-then-migrate-callers-incrementally**.
Rationale: spec explicitly warns option (b) is too high blast-radius for
this phase; live pipeline depends on multiple of the call-sites above.
