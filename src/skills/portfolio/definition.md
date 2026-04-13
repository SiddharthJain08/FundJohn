# Skill: /portfolio — Portfolio Snapshot

Flash mode — reads .agents/user/portfolio.json directly.
Displays:
- All positions with entry price, current price (live quote), P&L%
- Sector exposure breakdown
- Long%, Short%, Cash%
- last_verified_at staleness warning if >24h

Operator note: update portfolio.json manually after real trades.
Never auto-updates — operator-maintained only.

Usage: `!john /portfolio`
