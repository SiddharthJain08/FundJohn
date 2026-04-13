# Security Policy

## API Key Handling
- API keys live in .env only. Never in code, prompts, or committed files.
- Secret redaction middleware scrubs API keys from all tool results before they reach context.
- If you see an API key pattern in a tool result, do NOT repeat it in your response.

## Sandbox Boundaries
- Python execution is sandboxed (local or Daytona container per SANDBOX_TYPE).
- Write files only within the workspace directory. Never write to system paths.
- Do not execute shell commands that access the network directly — use MCP tools.
- No outbound HTTP from the sandbox except through the registered MCP tool calls.

## Portfolio State
- portfolio.json is READ-ONLY for all agents.
- Only the operator updates portfolio.json after executing real trades.
- Never write to portfolio.json. Never simulate portfolio state changes.
- If portfolio.json is stale (last_verified_at >24h), warn the operator but proceed with stale data clearly flagged.

## Trade Execution
- OpenClaw never executes trades. It recommends. Operator executes.
- Never connect to brokerage APIs, margin accounts, or clearing systems.
- Never post recommendations to external platforms or social media.

## Diligence Confidentiality
- Memos are for operator use only. Do not post to external URLs.
- Do not attach memos to public Discord channels.
- All results stay within the Discord server and workspace filesystem.

## Off Limits (absolute)
- Brokerage account access
- Real trade execution
- External posting of diligence content
- Overriding a BLOCKED risk decision (2+ check failures = non-negotiable)
