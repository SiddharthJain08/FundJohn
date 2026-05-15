"""Read-only Python client for DBnomics v22 REST API.

DBnomics is a free aggregator of IMF / ECB / BIS / OECD / World Bank macro series.
Public, no API key needed.  Net-new source vs Polygon+FMP+EDGAR.

Series IDs follow 'PROVIDER_CODE/DATASET_CODE/SERIES_CODE'.
Example: 'IMF/IFS/M.US.PCPI_PC_PP_PT' (US monthly CPI YoY %)."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from src.ingestion._http_retry import fetch_with_retry


class DBnomicsClient:
    BASE = "https://api.db.nomics.world/v22"

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def get_series(self, series_id: str, observations: bool = True) -> list[dict]:
        """Returns a list of observation dicts:
        [{provider_code, dataset_code, series_code, period, value}, ...]"""
        qs = urllib.parse.urlencode({
            "series_ids": series_id,
            "observations": "1" if observations else "0",
        })
        req = urllib.request.Request(
            f"{self.BASE}/series?{qs}",
            headers={"User-Agent": "OpenClaw-FundJohn/1.0 (+research)"},
        )
        body = fetch_with_retry(req, timeout=int(self.timeout), label='dbnomics')
        if body is None:
            raise RuntimeError("DBnomics fetch failed after retries")
        payload = json.loads(body)

        out = []
        for doc in payload.get("series", {}).get("docs", []):
            for period, value in zip(doc.get("period", []), doc.get("value", [])):
                out.append({
                    "provider_code": doc["provider_code"],
                    "dataset_code":  doc["dataset_code"],
                    "series_code":   doc["series_code"],
                    "period":        period,
                    "value":         value,
                })
        return out
