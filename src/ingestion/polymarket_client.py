"""Read-only Python client for Polymarket Gamma API (no auth required).

Used for prediction-market alt-data: macro-event probabilities, Fed/elections,
geopolitical binary outcomes.  Hands raw snapshots to the spike table for
MasterMind to evaluate as features."""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

# Same import dance as arxiv_discovery.py / openalex_discovery.py — bypass the
# broken src/ingestion/__init__.py and load _http_retry as a top-level module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _http_retry import fetch_with_retry  # noqa: E402


class PolymarketClient:
    GAMMA_BASE = "https://gamma-api.polymarket.com"

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def list_active_markets(self, limit: int = 50) -> list[dict]:
        """Returns active markets with normalized outcome prices."""
        qs = urllib.parse.urlencode({"active": "true", "closed": "false", "limit": limit})
        req = urllib.request.Request(
            f"{self.GAMMA_BASE}/markets?{qs}",
            headers={"User-Agent": "OpenClaw-FundJohn/1.0 (+research)"},
        )
        body = fetch_with_retry(req, timeout=int(self.timeout), label='polymarket')
        if body is None:
            raise RuntimeError("Polymarket fetch failed after retries")
        raw = json.loads(body)

        out = []
        for m in raw:
            prices_raw = m.get("outcomePrices")
            # Gamma API may return outcomePrices as a JSON-encoded string,
            # e.g. '["0.62", "0.38"]', instead of a real list. Normalize.
            if isinstance(prices_raw, str):
                try:
                    prices_raw = json.loads(prices_raw)
                except (ValueError, TypeError):
                    prices_raw = None
            if prices_raw is None:
                # Honest-missing rather than silent zero — a real "0% probability"
                # market would still ship explicit "0" strings, never a missing key.
                yes = no = None
            else:
                try:
                    yes = float(prices_raw[0])
                    no = float(prices_raw[1])
                except (ValueError, IndexError, TypeError):
                    yes = no = None
            out.append({
                "market_id":      m.get("id"),
                "question":       m.get("question"),
                "end_date":       m.get("endDate"),
                "yes_price":      yes,
                "no_price":       no,
                "volume_24h_usd": m.get("volume24hr"),
                "raw":            m,
            })
        return out
