# Skill: /watchlist — Watchlist Status

Flash mode — reads .agents/user/watchlist.json + verdict_cache files.
Displays:
- All watchlist tickers with latest verdict + score
- Signal status (GO/WAIT/PASS) from most recent report-builder output
- Staleness for each (days since analysis)
- Next earnings dates from FMP

Re-scan trigger: `!john /watchlist scan` → runs /screen for all stale names.

Usage: `!john /watchlist`
