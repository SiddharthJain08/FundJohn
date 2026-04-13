'use strict';

function generatePython(server) {
  return `# Auto-generated — Tavily search tool module
# ${server.description}
import os, requests
from _rate_limiter import _acquire_token

_API_KEY = os.environ.get("TAVILY_API_KEY", "")
_BASE = "https://api.tavily.com"
_PROVIDER = "tavily"

def _post(endpoint: str, payload: dict) -> dict:
    _acquire_token(_PROVIDER)
    r = requests.post(f"{_BASE}{endpoint}",
                      json={"api_key": _API_KEY, **payload},
                      timeout=30)
    r.raise_for_status()
    return r.json()

def search(query: str, max_results: int = 5, search_depth: str = "basic",
           include_domains: list = None, exclude_domains: list = None) -> dict:
    """Web search optimized for financial news and research.
    search_depth: 'basic' (fast) or 'advanced' (thorough, slower).
    """
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains
    return _post("/search", payload)

def search_news(ticker: str, company_name: str = None, max_results: int = 5) -> dict:
    """Search for recent news on a ticker/company."""
    query = f"{ticker} {company_name or ''} earnings news announcement".strip()
    return search(query, max_results=max_results, search_depth="basic",
                  include_domains=["reuters.com", "bloomberg.com", "wsj.com",
                                   "ft.com", "cnbc.com", "seekingalpha.com"])
`;
}

module.exports = { generatePython };
