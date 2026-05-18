"""Read-only Python client for DBnomics v22 REST API.

DBnomics is a free aggregator of IMF / ECB / BIS / OECD / World Bank macro series.
Public, no API key needed.  Net-new source vs Polygon+FMP+EDGAR.

Series IDs follow 'PROVIDER_CODE/DATASET_CODE/SERIES_CODE'.
Example: 'IMF/IFS/M.US.PCPI_PC_PP_PT' (US monthly CPI YoY %)."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import date

from src.ingestion._http_retry import fetch_with_retry


_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_HALF_RE    = re.compile(r"^(\d{4})-[HS]([12])$")
_WEEK_RE    = re.compile(r"^(\d{4})-W(\d{1,2})$")
_YEAR_RE    = re.compile(r"^(\d{4})$")
_MONTH_RE   = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_RE     = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _period_to_date(period: str, frequency: str | None = None) -> date | None:
    """Convert a DBnomics `period` string to the start-of-period DATE.

    Pure function — never raises. Returns None on anything we don't recognise
    so the caller can fall back to NULL period_start without crashing.

    Recognised forms:
      '2026'         -> date(2026, 1, 1)        (annual)
      '2026-S1'/'-H1' -> date(2026, 1, 1)        (semester / half)
      '2026-S2'/'-H2' -> date(2026, 7, 1)
      '2026-Q1'      -> date(2026, 1, 1)        (quarterly)
      '2026-Q2'      -> date(2026, 4, 1)
      '2026-Q3'      -> date(2026, 7, 1)
      '2026-Q4'      -> date(2026, 10, 1)
      '2026-01'      -> date(2026, 1, 1)        (monthly)
      '2026-W01'     -> ISO-week-1 Monday       (weekly)
      '2026-01-15'   -> date(2026, 1, 15)       (daily)
    """
    if not period or not isinstance(period, str):
        return None
    p = period.strip()

    m = _DAY_RE.match(p)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = _MONTH_RE.match(p)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            return None

    m = _QUARTER_RE.match(p)
    if m:
        year = int(m.group(1))
        q = int(m.group(2))
        month = (q - 1) * 3 + 1
        return date(year, month, 1)

    m = _HALF_RE.match(p)
    if m:
        year = int(m.group(1))
        h = int(m.group(2))
        month = 1 if h == 1 else 7
        return date(year, month, 1)

    m = _WEEK_RE.match(p)
    if m:
        year = int(m.group(1))
        week = int(m.group(2))
        if 1 <= week <= 53:
            try:
                # ISO week: Monday of that week
                return date.fromisocalendar(year, week, 1)
            except ValueError:
                return None
        return None

    m = _YEAR_RE.match(p)
    if m:
        return date(int(m.group(1)), 1, 1)

    return None


class DBnomicsClient:
    BASE = "https://api.db.nomics.world/v22"

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def get_series(self, series_id: str, observations: bool = True) -> list[dict]:
        """Returns a list of observation dicts:
        [{provider_code, dataset_code, series_code, period, value,
          period_start, frequency}, ...]

        period_start is a datetime.date (or None if the period string is
        unrecognised). frequency is the doc-level @frequency string from
        DBnomics ('monthly', 'quarterly', 'annual', ...) or None.
        """
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
            frequency = doc.get("@frequency")
            # DBnomics often ships a parallel 'period_start_day' array of ISO
            # dates already aligned to the period[] axis. Prefer it when present
            # — it's authoritative and avoids the fallback parser entirely.
            period_start_days = doc.get("period_start_day") or []
            periods = doc.get("period", [])
            values = doc.get("value", [])
            for i, (period, value) in enumerate(zip(periods, values)):
                period_start = None
                if i < len(period_start_days) and period_start_days[i]:
                    period_start = _period_to_date(period_start_days[i])
                if period_start is None:
                    period_start = _period_to_date(period, frequency)
                out.append({
                    "provider_code": doc["provider_code"],
                    "dataset_code":  doc["dataset_code"],
                    "series_code":   doc["series_code"],
                    "period":        period,
                    "value":         value,
                    "period_start":  period_start,
                    "frequency":     frequency,
                })
        return out
