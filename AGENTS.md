# OpenClaw — Agent Standing Orders

These are deterministic behavioral rules embedded in agent context — not LLM prompts.
No API calls, no token spend. Rules are enforced at the start of every operation.

---

## STANDING ORDERS

### SO-1: Budget Mode Gate
Check Redis key `budget:mode` before any non-essential PTC operation.
- **GREEN**: proceed normally
- **YELLOW**: reject non-essential PTC requests. Respond: "Budget YELLOW ($X/day) — only essential ops permitted. Use `/budget` for details."
- **RED**: reject all PTC requests except operator-manual triggers. Respond: "Budget RED — price collection only. Operator must approve PTC ops."

### SO-2: Unverified Pipeline Warning
If `pipeline_state` has any row where `current_stage = 'failed'` OR a run completed with `verdict_cache_written = FALSE`, prepend the next morning-note output with:
```
⚠️ UNVERIFIED RUN: Pipeline run {run_id} for {ticker} did not pass verification.
Verdict cache was NOT updated. Re-run diligence before acting on any cached verdict.
```

### SO-3: Integrity Violation Notice
If an `integrity-violation` SSE event was emitted within the last 24 hours (check Redis key `integrity:last_violation_ts`), prepend all diligence reports and morning notes with:
```
🔒 SECURITY NOTICE: File integrity check failed within the last 24h. 
Review SECURITY_ALERT logs before acting on this report.
```

### SO-4: Interrupted Run Alert
On any pipeline boot or `/status` query, surface all `pipeline_state` rows where `current_stage NOT IN ('complete','failed')` and `expired_at IS NULL`. Log as `INTERRUPTED_RUN`. Do NOT auto-resume. Operator decides.

### SO-5: Verdict Cache Integrity
Never write to `verdict_cache` unless all of the following are true:
1. Full pipeline ran to `report-builder` stage
2. All subagent outputs passed `verifySubagentOutput()` (no UNVERIFIED flags)
3. `pipeline_state.current_stage = 'complete'`

If any condition fails, set `verdict_cache_written = FALSE` and notify operator.

### SO-6: Rate Limit Respect
Before spawning any subagent, check `ratelimit:anthropic:reset_at` in Redis.
If provider is rate-limited, wait until `reset_at + 1s` before proceeding. Never skip this check.

---

## SCHEDULED OPERATIONS (deterministic only — no LLM calls)

| # | Schedule | Operation | Handler | Notes |
|---|---|---|---|---|
| 1 | Daily 06:00 ET (configurable) | Data collection pipeline | `collector.runDailyCollection()` | Phases 2a-6, budget-gated |
| 2 | Every 5min (market hours) | Price snapshots | `collector.runSnapshots()` | SP100 only, skipped in sleep mode |
| 3 | Every 45min | Claude auth sync | `bot.syncClaudeAuth()` | Copies credentials from /root/.claude/ |
| 4 | Every 60s | Rate bucket refill | `redis.initRateLimitBuckets()` | Token bucket reset per provider |
| 5 | Pipeline boot | Integrity verification | `integrity.verifyManifest()` | Hash check, SSE alert on mismatch |
| 6 | Pipeline boot | Interrupted run check | `pipeline-state.findInterruptedRuns()` | Surfaces to operator, no auto-resume |
| 7 | Daily cycle start | Budget check | `enforcer.checkBudget()` | Sets Redis budget:mode, zero LLM cost |
| 8 | Daily cycle start | Old run expiry | `pipeline-state.expireOldRuns()` | Marks runs >7d as expired |

**Total: 8 deterministic scheduled operations. Zero LLM calls.**

---

## DEPRECATED / REPLACED

The following were previously considered for cron implementation but are superseded:

| Was | Replaced by |
|---|---|
| Cost monitoring cron (LLM-based) | Budget enforcer (Change 6) — pure JS math |
| Daily reflection cron | Standing Orders SO-2/SO-3 — deterministic checks |
| Weekly cross-agent sync | Memory consolidation via dreaming system |
| promote-learnings cron | `/root/.learnings/` manual review + CLAUDE.md promotion |
| Token monitor (v1 file-based) | Budget enforcer (`src/budget/enforcer.js`) |

The `agents/token-monitor/` directory is legacy v1 — no active processes read from it.
