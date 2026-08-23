'use strict';

const FMP_BASE = 'https://financialmodelingprep.com/stable';

function generatePython(server) {
  return `# Auto-generated — FMP (Financial Modeling Prep) tool module
# ${server.description}
import os, sys, json, requests
from _rate_limiter import _acquire_token, _cycle_cache_get, _cycle_cache_set

_API_KEY = os.environ.get("FMP_API_KEY", "")
_BASE = "${FMP_BASE}"
_PROVIDER = "fmp"

# data_provider_health (2026-08-23): every call through this module is the
# dashboard's only view of FMP health. The recorder lives in the repo
# (src/maintenance/provider_health.py); this file runs from
# <root>/workspaces/<ws>/tools, so put the repo root on sys.path.
_ROOT = os.environ.get("OPENCLAW_ROOT") or os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    from src.maintenance import provider_health as _ph
except Exception:  # pragma: no cover — recording is best-effort
    _ph = None


class FmpSymbolGated(RuntimeError):
    """402 'Special Endpoint': this SYMBOL is gated behind a higher FMP tier
    (preferreds / warrants / units on Starter). Not a quota problem — skip the
    symbol and keep going. Subclasses RuntimeError so legacy handlers that
    catch the old 'free tier limit' error still work."""


def _record(endpoint, status, body):
    if _ph is None:
        return "ok" if status == 200 else "error"
    try:
        return _ph.record_http(_PROVIDER, endpoint, status, body)
    except Exception:
        return "error"


def _get(endpoint: str, params: dict = None) -> dict:
    # Cycle-cache: apikey deliberately excluded from cache key (it's a
    # constant per process and would just bloat the hash).
    cache_params = {"endpoint": endpoint, "params": params or {}}
    cached = _cycle_cache_get("fmp:get", cache_params)
    if cached is not None:
        return cached

    _acquire_token(_PROVIDER)
    p = {"apikey": _API_KEY, **(params or {})}
    try:
        r = requests.get(f"{_BASE}/{endpoint}", params=p, timeout=30)
    except Exception as exc:
        _record(endpoint, None, str(exc))
        raise
    kind = _record(endpoint, r.status_code, getattr(r, "text", ""))
    if kind == "symbol_gated":
        raise FmpSymbolGated(f"FMP: symbol {(params or {}).get('symbol')} is tier-gated on /{endpoint} (current subscription)")
    if kind == "quota":
        raise RuntimeError(f"FMP quota exhausted (402) on /{endpoint} — {getattr(r, 'text', '')[:120]}")
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
    # /stable/earnings = per-symbol report dates with epsActual/epsEstimated/
    # revenueActual/revenueEstimated (the old "surprises" path is 404 on
    # /stable/ — 2026-08-23).
    limit = min(limit, 4)
    return _get("earnings", {"symbol": ticker, "limit": limit})

def get_quote(ticker: str) -> dict:
    data = _get("quote", {"symbol": ticker})
    return data[0] if isinstance(data, list) and data else data

def get_historical_prices(ticker: str, limit: int = 252) -> list:
    return _get("historical-price-eod/full", {"symbol": ticker, "limit": limit})
`;
}

module.exports = { generatePython };
