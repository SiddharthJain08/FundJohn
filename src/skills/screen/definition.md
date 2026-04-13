# Skill: /screen — Watchlist Screen

Scan all tickers in watchlist.json against checklist thresholds.
For each ticker:
1. Check verdict_cache — use cached if fresh
2. If stale: spawn research + data-prep subagents
3. Run deterministic checklist
4. Rank by: verdict > score > EV/NTM

Output: ranked table of watchlist names with verdict and failing items.
Flag any names with new GO timing signals.

Usage: `!john /screen`
