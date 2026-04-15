"""
EDGAR Client — async wrapper with User-Agent enforcement and exponential backoff.
Required by SEC fair-use policy: every request must include a User-Agent header.

R6 implementation — FundJohn Pipeline Audit 2026-04-13
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

EDGAR_BASE         = "https://data.sec.gov"
EDGAR_SUBMISSIONS  = "https://data.sec.gov/submissions"
EDGAR_COMPANY_FACTS = "https://data.sec.gov/api/xbrl/companyfacts"

# SEC requires: "CompanyName AdminContact@email.com"
USER_AGENT = "FundJohn/OpenClaw contact@fundjohn.ai"

# Exponential backoff schedule (seconds)
RETRY_DELAYS = [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
RATE_LIMIT_DELAY = 0.1   # 10 req/sec → 100 ms between calls


class EDGARClient:
    """
    Async EDGAR client with:
      - User-Agent header on every request (SEC compliance)
      - 10 req/sec self-throttling
      - Exponential backoff on 429 / 5xx
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_call: float = 0.0

    async def __aenter__(self) -> "EDGARClient":
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": USER_AGENT,
                "Accept":     "application/json",
            }
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self._session:
            await self._session.close()

    async def _throttle(self) -> None:
        """Enforce 10 req/sec rate limit."""
        elapsed = time.monotonic() - self._last_call
        if elapsed < RATE_LIMIT_DELAY:
            await asyncio.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_call = time.monotonic()

    async def get(self, url: str, params: Optional[Dict] = None) -> Optional[Any]:
        """GET with exponential backoff on 429/5xx."""
        for attempt, delay in enumerate(RETRY_DELAYS):
            await self._throttle()
            try:
                async with self._session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    elif resp.status == 429:
                        logger.warning("EDGAR 429 rate-limited, backing off %.1fs (attempt %d)",
                                       delay, attempt + 1)
                        await asyncio.sleep(delay)
                    elif resp.status >= 500:
                        logger.warning("EDGAR 5xx (%d), backing off %.1fs", resp.status, delay)
                        await asyncio.sleep(delay)
                    else:
                        logger.error("EDGAR HTTP %d for %s", resp.status, url)
                        return None
            except asyncio.TimeoutError:
                logger.warning("EDGAR timeout on %s, backing off %.1fs", url, delay)
                await asyncio.sleep(delay)
            except Exception as exc:
                logger.error("EDGAR request error: %s", exc)
                if attempt == len(RETRY_DELAYS) - 1:
                    raise
                await asyncio.sleep(delay)

        logger.error("EDGAR: exhausted retries for %s", url)
        return None

    async def get_submissions(self, cik: str) -> Optional[Dict]:
        """Fetch company submission history. CIK zero-padded to 10 digits."""
        cik_padded = str(cik).zfill(10)
        return await self.get(f"{EDGAR_SUBMISSIONS}/CIK{cik_padded}.json")

    async def get_company_facts(self, cik: str) -> Optional[Dict]:
        """Fetch XBRL company facts (structured financials) by CIK."""
        cik_padded = str(cik).zfill(10)
        return await self.get(f"{EDGAR_COMPANY_FACTS}/CIK{cik_padded}.json")

    async def get_filing_index(self, cik: str, accession: str) -> Optional[Dict]:
        """Fetch index for a specific accession number."""
        acc_clean = accession.replace("-", "")
        url = (f"{EDGAR_BASE}/Archives/edgar/full-index"
               f"/{cik}/{accession}/{acc_clean}-index.json")
        return await self.get(url)


async def fetch_filings(cik: str, form_types: Optional[List[str]] = None) -> List[Dict]:
    """
    Convenience function: fetch recent filings for a CIK.
    Returns list filtered by form_types (e.g. ['10-K', '10-Q']).
    """
    async with EDGARClient() as client:
        data = await client.get_submissions(cik)

    if not data:
        return []

    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    forms      = recent.get("form", [])
    dates      = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])

    results = []
    for form, date_str, acc in zip(forms, dates, accessions):
        if form_types is None or form in form_types:
            results.append({"form": form, "date": date_str,
                            "accession": acc, "cik": cik})
    return results
