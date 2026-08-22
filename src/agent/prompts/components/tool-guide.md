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
from tools.fmp import get_financial_statements, get_key_metrics, get_profile, get_ratios, get_historical_prices  # P2: fundamentals + macro + historical prices
from tools.sec_edgar import get_filing, search_filings, get_submissions, get_company_facts  # canonical: insider / Form 4 / filings
from tools.tavily import search  # news/web
from tools.alpaca import get_bars, get_snapshots, get_latest_trades, get_news, get_option_chain, get_corporate_actions, get_screener_movers, get_screener_most_actives, get_positions, get_open_orders, get_account, get_clock, get_calendar  # P1: quotes/OHLCV, options chain, news, screener, corp-actions, broker state (READ-ONLY — no order functions)
# tools.alpaca wraps the alpaca CLI (/root/go/bin/alpaca) with --quiet/--timeout, multi-symbol
# batching, next_page_token pagination and the per-cycle cache. Raises AlpacaAuthError (rc=2,
# never retry) / AlpacaCLIError(status, code, error, hint). Execution stays in alpaca_executor.py.

# Always use _call_mcp() — it handles rate limiting automatically
prices = get_prices(ticker="AAPL", from_date="2024-01-01", to_date="2024-12-31")
data = get_financial_statements(ticker="AAPL", period="quarterly", limit=4)
```

## MCP Provider Routing

| Data Type | P1 | P2 |
|-----------|----|----|
| Prices / OHLCV | tools.alpaca get_bars / get_snapshots (AAT Plus) | fmp (get_historical_prices) |
| Options chain | tools.alpaca get_option_chain (AAT Plus) | — |
| News | tools.alpaca get_news (AAT Plus) | tavily |
| Screener / movers | tools.alpaca get_screener_movers / get_screener_most_actives | — |
| Corporate actions | tools.alpaca get_corporate_actions | — |
| Broker state (orders/positions/account) | tools.alpaca get_positions / get_open_orders / get_account (read-only) | — |
| Fundamentals / ratios | fmp | — |
| Sector performance | fmp | — |
| Macro (GDP, CPI, rates) | fmp | — |
| Insider transactions / Form 4 | sec_edgar | — |
| Filings (10-K, 10-Q, 8-K) | sec_edgar | — |
| Web research / press releases | tavily | — |

> **Note (2026-05-22 SP-1)**: Polygon and Yahoo fully removed from the data
> stack. Alpaca AAT Plus is now P1 for equity quotes, options chain, news,
> screener, and corporate actions (via alpaca CLI subprocess). FMP Starter
> covers fundamentals, macro, and historical prices. SEC EDGAR is canonical
> for insider transactions and filings.

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
