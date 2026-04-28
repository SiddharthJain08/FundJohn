# Tool Guide

## Decision Tree: Which tool path to use?

```
Direct question (price, status, quote)?
  → JSON snapshot tool (quote.js, profile.js, market-status.js)
  → Return immediately, no Python

Compute, compare, model, or screen?
  → Write Python using MCP imports
  → Execute in sandbox
  → Read output back

Multi-source data synthesis?
  → data-prep subagent → compute subagent
  → Python does the joins, not context

Full diligence?
  → Spawn research + data-prep in parallel
  → Validate → compute → equity-analyst → report-builder
```

## PTC Pattern (Python Tool Calling)

When you need data from MCP providers, write Python — do NOT call HTTP yourself:

```python
from tools.polygon import get_snapshot, get_prices, get_sma, get_rsi, get_sector_performance  # primary
from tools.fmp import get_financial_statements, get_key_metrics, get_profile, get_economic_calendar
from tools.sec_edgar import get_filing, search_filings
from tools.yahoo import get_insider_transactions, get_short_interest  # fallback only
from tools.tavily import search  # news/web

# Always use _call_mcp() — it handles rate limiting automatically
prices = get_prices(ticker="AAPL", from_date="2024-01-01", to_date="2024-12-31")
data = get_financial_statements(ticker="AAPL", period="quarterly", limit=4)
```

## MCP Provider Routing

| Data Type | Tier 1 | Tier 2 Fallback |
|-----------|--------|-----------------|
| Prices / OHLCV | polygon | yahoo |
| Technical indicators | polygon | — |
| Fundamentals | fmp | yahoo |
| Filings | sec_edgar | — |
| News / sentiment | tavily | — |
| Options / IV | polygon, yahoo | — |
| Broker state (orders/positions/fills) | alpaca CLI | — |
| Watchlist / screener | alpaca CLI | — |
| Corporate actions | alpaca CLI | — |
| Macro (GDP, CPI, rates) | fmp | — |

> **Note (2026-04-28)**: AlphaVantage was removed from the data stream.
> Polygon now covers technical indicators (RSI/SMA/EMA/MACD/BBands) and
> sector performance; FMP covers macro and economic calendar; Alpaca's
> CLI covers broker-state + watchlist + screener + corporate actions.

## Snapshot Tools (no Python, instant response)
- `quote.js` — real-time price, volume, change%
- `profile.js` — company name, sector, market cap
- `earnings-calendar.js` — next N earnings dates
- `market-status.js` — market open/closed, next open

## Rules
- Never call HTTP APIs directly. Use the generated tool modules.
- Never put raw API responses in context — always process in Python first.
- All tool calls route through the Redis rate limiter automatically via _call_mcp().
- If a tool returns an error, check the fallback chain in preferences.json before giving up.
