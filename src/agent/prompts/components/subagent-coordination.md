# Subagent Coordination

## Swarm Principles
- Research subagents run in PARALLEL. Never serialize what can run concurrently.
- Trade pipeline subagents run SEQUENTIALLY (data-prep → compute → equity-analyst → report-builder).
- Each subagent has isolated LLM context but shares the workspace filesystem.
- Results written to work/<task>/data/ by one subagent are visible to all others.

## Before Spawning — Check Cache
1. Query PostgreSQL verdict_cache WHERE ticker = ? AND stale_after > NOW()
2. Check .agents/verdict-cache/{ticker}-{date}.json filesystem cache
3. Apply staleness windows from preferences.json:
   - Research: 7 days
   - Prices: 24 hours
   - Statements: 30 days
   - Compute/EV: 24 hours
4. Only spawn subagents whose data is stale or missing.
5. Log the plan to agent.md.

## Spawning Pattern
```javascript
// Parallel research
const [researchResult] = await swarm.parallel([
  swarm.init({ type: "research", ticker, workspace }),
]);

// Sequential trade pipeline (after research)
await swarm.runTradePipeline(ticker, workspace, taskDir);
```

## Mid-Run Steering
- Operator can send follow-up `!john` messages while subagents run.
- Messages go into Redis steering queue keyed by thread ID.
- Middleware drains queue before each LLM call — agent sees messages naturally.
- Use `swarm.update(subagentId, message)` to redirect a running subagent.

## Resume
- All subagent states checkpointed to PostgreSQL.
- On reconnect, use `swarm.resume(checkpointId, context)` to rehydrate.

## Status Reporting
- `!john /status` reads from Redis subagent status keys.
- Format: `{subagentType} [{ticker}] — {status} | elapsed: {Ns}`

## Subagent Types
| Type | Runs | Parallel? | Veto? |
|------|------|-----------|-------|
| research | Bull + Bear analysis | Yes | No |
| data-prep | Revenue quality + data gathering | Yes (with research) | No |
| equity-analyst | Mgmt + Filings + Portfolio risk | After data-prep | YES |
| compute | EV + sizing + scenarios | After data-prep | No |
| report-builder | Memo + trade report assembly | Last | No |
