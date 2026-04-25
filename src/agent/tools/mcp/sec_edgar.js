'use strict';

function generatePython(server) {
  return `# Auto-generated — SEC EDGAR tool module
# ${server.description}
import os, requests
from _rate_limiter import _acquire_token, _cycle_cache_get, _cycle_cache_set

_USER_AGENT = os.environ.get("SEC_USER_AGENT", "OpenClaw/1.0 (contact@example.com)")
_BASE = "https://data.sec.gov"
_PROVIDER = "sec_edgar"

def _get(path: str, params: dict = None) -> dict:
    # Cycle-cache around JSON endpoints (submissions, companyfacts,
    # companyconcept). Note: get_filing() returns raw HTML/text and is
    # NOT cached here — large text would blow up Redis values.
    cache_params = {"path": path, "params": params or {}}
    cached = _cycle_cache_get("edgar:get", cache_params)
    if cached is not None:
        return cached

    _acquire_token(_PROVIDER)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    r = requests.get(f"{_BASE}{path}", params=params or {}, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    _cycle_cache_set("edgar:get", cache_params, data)
    return data

def get_submissions(cik: str) -> dict:
    """Company submissions + recent filing index. CIK must be zero-padded to 10 digits."""
    cik_padded = str(cik).zfill(10)
    return _get(f"/submissions/CIK{cik_padded}.json")

def get_company_facts(cik: str) -> dict:
    """All XBRL facts for a company (financials in structured form)."""
    cik_padded = str(cik).zfill(10)
    return _get(f"/api/xbrl/companyfacts/CIK{cik_padded}.json")

def get_company_concept(cik: str, concept: str, unit: str = "USD") -> dict:
    """Single XBRL concept for a company. Example concept: us-gaap/Revenues"""
    cik_padded = str(cik).zfill(10)
    return _get(f"/api/xbrl/companyconcept/CIK{cik_padded}/{concept}.json")

def search_filings(company_name: str = None, ticker: str = None, form_type: str = "10-K") -> dict:
    """Full-text search for filings via EDGAR full-text search."""
    params = {"type": form_type, "dateb": "", "owner": "include", "count": "10", "search_text": ""}
    if ticker:
        params["action"] = "getcompany"
        params["company"] = ticker
    url = "https://efts.sec.gov/LATEST/search-index"
    _acquire_token(_PROVIDER)
    headers = {"User-Agent": _USER_AGENT}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def get_filing(accession_number: str, cik: str) -> str:
    """Fetch full text of a specific filing by accession number."""
    cik_padded = str(cik).zfill(10)
    acc = accession_number.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_padded}/{acc}/"
    _acquire_token(_PROVIDER)
    headers = {"User-Agent": _USER_AGENT}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text
`;
}

module.exports = { generatePython };
