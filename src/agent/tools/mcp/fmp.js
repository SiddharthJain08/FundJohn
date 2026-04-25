'use strict';

const FMP_BASE = 'https://financialmodelingprep.com/stable';

function generatePython(server) {
  return `# Auto-generated — FMP (Financial Modeling Prep) tool module
# ${server.description}
import os, json, requests
from _rate_limiter import _acquire_token, _cycle_cache_get, _cycle_cache_set

_API_KEY = os.environ.get("FMP_API_KEY", "")
_BASE = "${FMP_BASE}"
_PROVIDER = "fmp"

def _get(endpoint: str, params: dict = None) -> dict:
    # Cycle-cache: apikey deliberately excluded from cache key (it's a
    # constant per process and would just bloat the hash).
    cache_params = {"endpoint": endpoint, "params": params or {}}
    cached = _cycle_cache_get("fmp:get", cache_params)
    if cached is not None:
        return cached

    _acquire_token(_PROVIDER)
    p = {"apikey": _API_KEY, **(params or {})}
    r = requests.get(f"{_BASE}/{endpoint}", params=p, timeout=30)
    if r.status_code == 402:
        raise RuntimeError(f"FMP free tier limit hit on /{endpoint} — upgrade plan or reduce limit param")
    r.raise_for_status()
    data = r.json()
    _cycle_cache_set("fmp:get", cache_params, data)
    return data

def get_profile(ticker: str) -> dict:
    """Company profile: name, sector, market cap, CIK, description."""
    data = _get("profile", {"symbol": ticker})
    return data[0] if isinstance(data, list) and data else data

def get_financial_statements(ticker: str, period: str = "quarterly", limit: int = 4) -> list:
    """Income statement, balance sheet, cash flow for last N periods (max 4 on free tier)."""
    limit = min(limit, 4)
    return _get("income-statement", {"symbol": ticker, "period": period, "limit": limit})

def get_balance_sheet(ticker: str, period: str = "quarterly", limit: int = 4) -> list:
    limit = min(limit, 4)
    return _get("balance-sheet-statement", {"symbol": ticker, "period": period, "limit": limit})

def get_cash_flow(ticker: str, period: str = "quarterly", limit: int = 4) -> list:
    limit = min(limit, 4)
    return _get("cash-flow-statement", {"symbol": ticker, "period": period, "limit": limit})

def get_key_metrics(ticker: str, limit: int = 4) -> list:
    limit = min(limit, 4)
    return _get("key-metrics", {"symbol": ticker, "limit": limit})

def get_ratios(ticker: str, limit: int = 4) -> list:
    limit = min(limit, 4)
    return _get("ratios", {"symbol": ticker, "limit": limit})

def get_peers(ticker: str) -> list:
    return _get("stock-peers", {"symbol": ticker})

def get_price_target(ticker: str) -> dict:
    data = _get("price-target-consensus", {"symbol": ticker})
    return data[0] if isinstance(data, list) and data else data

def get_earnings_calendar(ticker: str, limit: int = 4) -> list:
    limit = min(limit, 4)
    return _get("earnings-surprises", {"symbol": ticker, "limit": limit})

def get_quote(ticker: str) -> dict:
    data = _get("quote", {"symbol": ticker})
    return data[0] if isinstance(data, list) and data else data

def get_historical_prices(ticker: str, limit: int = 252) -> list:
    return _get("historical-price-eod/full", {"symbol": ticker, "limit": limit})
`;
}

module.exports = { generatePython };
