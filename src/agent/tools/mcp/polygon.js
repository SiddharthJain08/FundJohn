'use strict';

function generatePython(server) {
  return `# Auto-generated — Polygon.io tool module
# ${server.description}
import os, requests
from _rate_limiter import _acquire_token, _cycle_cache_get, _cycle_cache_set

_API_KEY = os.environ.get("POLYGON_API_KEY", "")
_BASE = "https://api.polygon.io"
_PROVIDER = "polygon"

def _get(path: str, params: dict = None) -> dict:
    # Cycle-cache layer: when CYCLE_ID is set (LangGraph cycle path), the
    # second-Nth subagent in the same cycle hits Redis instead of refetching.
    # Transparent passthrough when CYCLE_ID is unset.
    cache_params = {"path": path, "params": params or {}}
    cached = _cycle_cache_get("polygon:get", cache_params)
    if cached is not None:
        return cached

    _acquire_token(_PROVIDER)
    headers = {"Authorization": f"Bearer {_API_KEY}"}
    r = requests.get(f"{_BASE}{path}", params=params or {}, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    _cycle_cache_set("polygon:get", cache_params, data)
    return data

def get_snapshot(ticker: str) -> dict:
    """Real-time snapshot: price, volume, VWAP, open/close."""
    return _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")

def get_prices(ticker: str, from_date: str, to_date: str, timespan: str = "day") -> dict:
    """OHLCV price bars. timespan: minute, hour, day, week, month."""
    return _get(f"/v2/aggs/ticker/{ticker}/range/1/{timespan}/{from_date}/{to_date}",
                params={"adjusted": "true", "sort": "asc", "limit": 5000})

def get_daily_open_close(ticker: str, date: str) -> dict:
    """Daily open, close, high, low for a specific date."""
    return _get(f"/v1/open-close/{ticker}/{date}", params={"adjusted": "true"})

def get_ticker_details(ticker: str) -> dict:
    """Company details: name, sector, SIC, market cap, shares outstanding."""
    return _get(f"/v3/reference/tickers/{ticker}")

def get_market_movers(direction: str = "gainers") -> dict:
    """Market movers. direction: gainers, losers."""
    return _get(f"/v2/snapshot/locale/us/markets/stocks/{direction}")

def get_sma(ticker: str, window: int = 50, timespan: str = "day", limit: int = 100) -> dict:
    return _get(f"/v1/indicators/sma/{ticker}",
                params={"timespan": timespan, "window": window, "limit": limit, "adjusted": "true"})

def get_rsi(ticker: str, window: int = 14, timespan: str = "day", limit: int = 100) -> dict:
    return _get(f"/v1/indicators/rsi/{ticker}",
                params={"timespan": timespan, "window": window, "limit": limit, "adjusted": "true"})
`;
}

module.exports = { generatePython };
