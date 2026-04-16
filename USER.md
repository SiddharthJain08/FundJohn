# USER.md — Who Am I?

## Identity
- **Role**: Solo portfolio manager, founder, and operator of the OpenClaw / FundJohn system
- **Timezone**: EST (default)
- **Pronouns**: He/him
- **Business**: OpenClaw is an autonomous quantitative hedge fund system built on Claude Code, operated through Discord via BotJohn

## Current System
- FundJohn bot-network: 3-agent quant PM system (BotJohn, ResearchJohn, TradeJohn) + hardcoded data pipeline
- 10 active strategies across live/paper/deprecated lifecycle states
- Strategy lifecycle managed via lifecycle.py + manifest.json
- Data pipeline: parquet-based master datasets (prices, financials, options, macro, insider)

## Tech Stack
- Claude Code (primary development and execution environment)
- Discord (command interface via BotJohn)
- Python (strategies, data pipeline, lifecycle management)
- Node.js (Discord bot, orchestrator)
- Git (version control, deployment via git pull on VPS)
- MCP servers: Yahoo Finance, Polygon, FMP, SEC EDGAR, Alpha Vantage

## Work Style
- Work hours are irregular — commands can come at any time
- Prefers concise, action-oriented responses
- Generates paste commands rather than having agents type into terminals
- Expects agents to check file state before modifying anything
- No hand-holding — agents figure it out and report back

## Operator Channels (Discord)
- `#ops` — system health, errors, lifecycle events
- `#research` — ResearchJohn reports
- `#signals` — TradeJohn trade signals
- `#data-alerts` — data pipeline collection and deployment status
