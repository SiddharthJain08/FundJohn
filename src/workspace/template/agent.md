# OpenClaw Workspace Memory

## Operator Profile
- Communication: concise, direct, no filler. Tables for data, markdown for memos.
- Default bearish. Look for the kill before the thesis.
- Always rank, never list. Always show raw numbers alongside %.
- Frustrations: disclaimers, hedging, preamble, "I'd be happy to help."
- Push back when data contradicts thesis.

## Diligence Framework
- 6-item checklist runs as deterministic code. Thresholds from preferences.json.
- Verdict: 6/6 pass → PROCEED | 1-2 fail → REVIEW | 3+ fail → KILL
- Kill criteria: insider > threshold, 3+ failures, material accounting flags
- Multiples: EV/NTM Revenue (growth), EV/EBITDA (mature), P/E (sanity check)

## Trade Framework
- Every trade: EV calculation, half-Kelly sizing (fraction=0.5), hard ceiling at max_position_pct
- Risk checks use thresholds from preferences.json. All 6 checks must pass for full size.
- Equity-analyst has veto. 2+ risk fails → BLOCKED (no escalation). 1 fail → REDUCED + escalation.
- Escalation: operator ONLINE → [TRADE REVIEW REQUIRED]. Operator OFFLINE → [PENDING REVIEW] queue.
- Portfolio state is READ-ONLY. Check last_verified_at before every risk check. Warn if >24h stale.

## Data Pipeline Rules (LOCKED)
- data-prep must produce DATA_MANIFEST.json before pipeline proceeds.
- validate_manifest() is called automatically before compute and equity-analyst.
- If validation fails: pipeline halts, error reported to operator. No workarounds.
- All MCP calls go through shared Redis token bucket. Never bypass the rate limiter.
- If a Tier 1 provider is exhausted: fall back per fallback_chain in preferences.json. Log it.

## Architecture Decisions (LOCKED — do not revisit)
- Discord bot: discord.js v14
- Execution: Claude Code with --dangerously-skip-permissions
- Model: claude-sonnet-4-6 for subagents
- Subagent types: research, data-prep, equity-analyst, report-builder, compute
- PTC-first: all data processing in Python, never raw JSON in context
- Rate limiter: Redis token bucket shared across all subagents
- Validation gate: mandatory between data-prep → compute and data-prep → equity-analyst

## Verdict Cache
- report-builder writes structured JSON to .agents/verdict-cache/{ticker}-{date}.json after every run.
- Planner checks verdict-cache before spawning subagents. If stale_after > now: reuse.
- Query pattern: "show me all REVIEW names where checklist.concentration = FAIL" → read cache files.

## Lessons Learned
- SEC EDGAR: 10 req/sec, must set User-Agent header or silent fail. Rate limiter enforces this.
- Yahoo Finance: 15-20 min lag on prices. Always note timestamp. Use as fallback only.
- FMP free tier: 250 req/day. Starter ($14/mo) for 300/min. Set per_day in preferences.json.
- Alpha Vantage free: 25 req/day. Paid for 75/min. Set per_minute in preferences.json.
- FMP API: use /stable/ endpoints (v3 deprecated August 2025). Query-param format for symbols.
- Half-Kelly with max_position_pct ceiling is non-negotiable. Full Kelly ruins funds.
- Checklist is deterministic code, not an LLM call.
- Agent key-value output saves ~60% tokens vs prose.
- Never run subagents sequentially when they can be parallel.
- Never store API keys in committed files. .env only.
- Never overwrite memos. Use {ticker}-{date} naming.
- Never trust management commentary at face value.
- Anomalous API data (zero revenue, negative EV) will propagate silently if not caught. The validation gate exists for this reason.

## Goals
(Agent updates this section per workspace)

## Active Threads
(Agent maintains thread index here)

## File Index
(Agent maintains file index here)

## Strategist Agent

### Activation Conditions
All three must be true (RISK_SCAN bypasses all):
1. Off-hours (6pm–6am ET weekdays, all weekend)
2. >= 20% daily token budget remaining
3. Pipeline idle (no other subagents running)

### Yield Behavior
- Saves state and exits immediately when pipeline becomes active
- Pauses when token budget drops below 20%
- Resumes exactly where it left off on next eligible session
- RISK_SCAN mode ignores all constraints — emergency use only

### Session Status
- last_session: (updated by strategist)
- hypotheses_explored_total: 0
- hypotheses_validated_total: 0
- reports_published_total: 0
- highest_sharpe_found: 0

### Published Strategies Pending Review
(strategist updates — operator marks IMPLEMENTED or REJECTED)

### Emergency Alerts History
(strategist logs all alerts)

### Operator Approvals Needed
(new datasets added by strategist requiring /approve-dataset)
