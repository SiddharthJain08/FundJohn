'use strict';

function generatePython(server) {
  return `# Auto-generated — Yahoo Finance (Tier 2 fallback) tool module
# ${server.description}
# WARNING: Yahoo Finance is unofficial. Use only when Tier 1 providers are unavailable.
import requests, time
from _rate_limiter import _acquire_token, _cycle_cache_get, _cycle_cache_set

_PROVIDER = "yahoo"
_CRUMB = None
_COOKIES = None
_CRUMB_FETCHED_AT = 0
_CRUMB_TTL = 1800  # 30 minutes

def _refresh_crumb():
    global _CRUMB, _COOKIES, _CRUMB_FETCHED_AT
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    # Get cookies
    session.get("https://finance.yahoo.com", timeout=10)
    # Get crumb
    r = session.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10)
    if r.status_code == 200:
        _CRUMB = r.text.strip()
        _COOKIES = session.cookies.get_dict()
        _CRUMB_FETCHED_AT = time.time()

def _get_crumb():
    if not _CRUMB or (time.time() - _CRUMB_FETCHED_AT) > _CRUMB_TTL:
        _refresh_crumb()
    return _CRUMB, _COOKIES

def _get(url: str, params: dict = None) -> dict:
    # Cycle-cache: crumb deliberately excluded from cache key — it rotates
    # every 30 minutes per session but Yahoo accepts any valid crumb for
    # any session, so it's not part of the response identity.
    cache_params = {"url": url, "params": params or {}}
    cached = _cycle_cache_get("yahoo:get", cache_params)
    if cached is not None:
        return cached

    _acquire_token(_PROVIDER)
    crumb, cookies = _get_crumb()
    p = {**(params or {}), "crumb": crumb}
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, params=p, cookies=cookies, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    _cycle_cache_set("yahoo:get", cache_params, data)
    return data

def get_quote(ticker: str) -> dict:
    """Fallback quote from Yahoo Finance."""
    data = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                {"interval": "1d", "range": "1d"})
    return data.get("chart", {}).get("result", [{}])[0]

def get_options(ticker: str) -> dict:
    """Options chain (calls + puts) — not available on FMP/Polygon free tiers."""
    return _get(f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}")

def get_insider_transactions(ticker: str) -> dict:
    """Insider transactions (Form 4 proxy) — fallback to sec_edgar preferred."""
    return _get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                {"modules": "insiderTransactions"})

def get_short_interest(ticker: str) -> dict:
    """Short interest ratio and shares short."""
    return _get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                {"modules": "defaultKeyStatistics"})
`;
}

module.exports = { generatePython };
