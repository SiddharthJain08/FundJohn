# MEMORY.md — What Should BotJohn Always Know?

## Preferences
- Node.js for all tooling (bot, orchestrator, scripts)
- Markdown for all memo output
- Tables over prose for financial data
- Always rank, never just list
- Default bearish — look for the kill before the thesis

## Projects

### OpenClaw (Active — Primary)
- Status: Architecture built, identity files being initialized
- Stack: Claude Code + Discord + Node.js + MCP servers
- Conventions: All skills in .claude/commands/, all output to output/memos/, all configs in .env
- Key files: CLAUDE.md (diligence brain), .mcp.json (data pipeline), johnbot/index.js (Discord bot), scripts/orchestrator.js (convergence layer)

## People & Clients
- No external clients yet. BotJohn operates for the sole operator.
- No team members. BotJohn IS the team, along with the 5 sub-agents.

## Decisions Already Made — Do Not Revisit
1. **Architecture**: 7-layer system (Discord → CLAUDE.md → Skills → MCP → Sub-Agents → Orchestrator → Memo). This is locked.
2. **Bot framework**: discord.js v14. Do not suggest alternatives.
3. **Execution model**: Claude Code child processes with --dangerously-skip-permissions. This is intentional.
4. **Model**: claude-haiku-4-5-20251001 for sub-agents for speed. Do not change without being asked.
5. **Checklist**: 6 items exactly. Do not add or remove items without explicit instruction.
6. **Verdict logic**: All pass → PROCEED, 1-2 fail → REVIEW, 3+ fail → KILL. Locked.

## Lessons Learned
- SEC EDGAR rate limits are 10 req/sec with User-Agent header. Always set the header or requests silently fail.
- Yahoo Finance data can lag by 15-20 minutes. Always note the timestamp.
- Long Claude Code runs (>3 min) should have progress logging so !john /status can report accurately.
- Git worktrees need clean working directories. Always stash or commit before creating scenario branches.
- Discord messages over 2000 chars must be sent as file attachments, not split into multiple messages.
- FMP free tier: 250 requests/day. Upgrade to starter ($14/mo) for 300/min if running >5 diligence checks/day.
- Alpha Vantage free tier: 25 requests/day. Upgrade to $49.99/mo for 75/min for active use.
- Yahoo Finance (yfinance) has no official rate limit but throttles after ~2000 requests/hour — keep as fallback only.
- Agent key-value block output format saves ~60% tokens vs markdown prose. Never revert to verbose output.
- Checklist evaluation is deterministic code in orchestrator.js, not an LLM call. Never route it through Claude.
- Data router caching (5-min TTL) prevents redundant API calls when multiple agents query the same ticker in parallel.
- `alertPct` in token-budget.js must be an array, not a number. Passing `alertPct: 80` (number) causes `TypeError: 80 is not iterable` inside `recordUsage()`, crashing every orchestrator silently after the first agent completes.

## Mistakes to Avoid
- Do not run all 5 sub-agents sequentially. They MUST run in parallel or diligence takes too long.
- Do not store API keys in any committed file. .env only, .gitignored.
- Do not overwrite existing memos. Use {ticker}-{date}.md naming to preserve history.
- Do not trust management commentary at face value. Always cross-reference with /mgmt-scorecard data.
