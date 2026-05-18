"""Polygon adapter — wraps the existing snapshot-endpoint call shape.

Upstream call shape mirrored from:
  src/agent/tools/mcp/polygon.js -> get_snapshot
  src/ingestion/pipeline.py      -> POLYGON_BASE + bearer-token auth
  src/pipeline/collector.js      -> /v3/snapshot/options/{T}

For equity quote we hit /v2/snapshot/locale/us/markets/stocks/tickers/{T}
which is the Polygon endpoint that returns lastTrade.p + lastQuote.{bp,ap}
in a single round-trip. Authorization: Bearer header per polygon.js.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable

from src.ingestion._http_retry import fetch_with_retry
from src.ingestion import quote_sources as _registry
from src.ingestion.quote_monitor import Quote


class PolygonQuoteSource:
    """Per-ticker snapshot fan-out. One HTTP call per ticker (Polygon's
    bulk endpoint requires a paid Advanced subscription we don't have)."""

    name = 'polygon'
    priority = 0  # Polygon is primary in production; wins ties

    BASE = 'https://api.polygon.io'

    def __init__(self, api_key: str | None = None, timeout: int = 5):
        # Distinguish None (caller didn't pass anything → use env) from ''
        # (caller explicitly passed empty → disable). `api_key or env_get`
        # treats both as falsy, so a test that wants to assert no-creds
        # behavior (api_key='') would still pick up POLYGON_API_KEY from
        # the live env.
        self.api_key = api_key if api_key is not None else os.environ.get('POLYGON_API_KEY', '')
        self.timeout = timeout

    async def fetch(self, tickers: Iterable[str]) -> dict[str, Quote]:
        if not self.api_key:
            return {}
        # Fan out per-ticker concurrently using asyncio.to_thread for the
        # blocking urllib call. The orchestrator's per-source 3s ack timeout
        # is the upper bound on the whole batch.
        ticker_list = list(tickers)
        results = await asyncio.gather(
            *(asyncio.to_thread(self._fetch_one, t) for t in ticker_list),
            return_exceptions=True,
        )
        out: dict[str, Quote] = {}
        for t, r in zip(ticker_list, results):
            if isinstance(r, Quote):
                out[t] = r
            # else: dropped silently — upstream None / exception. Orchestrator
            # handles fallback via other sources.
        return out

    def _fetch_one(self, ticker: str) -> Quote | None:
        path = f'/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}'
        req = urllib.request.Request(
            f'{self.BASE}{path}',
            headers={'Authorization': f'Bearer {self.api_key}'},
        )
        body = fetch_with_retry(req, timeout=self.timeout, label=f'polygon-snap-{ticker}')
        if body is None:
            return None
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return None
        ticker_obj = (data or {}).get('ticker') or {}
        last_trade = ticker_obj.get('lastTrade') or {}
        last_quote = ticker_obj.get('lastQuote') or {}
        day        = ticker_obj.get('day') or {}
        # Prefer lastTrade.p (matches Alpaca's order-validation logic the
        # bracket-snap in alpaca_executor.py already relies on). Fall back
        # to quote midpoint, then to day close.
        trade_p = _safe_float(last_trade.get('p'))
        bid     = _safe_float(last_quote.get('bp'))
        ask     = _safe_float(last_quote.get('ap'))
        price = trade_p
        if price is None:
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                price = (bid + ask) / 2.0
            else:
                price = _safe_float(day.get('c'))
        if price is None or price <= 0:
            return None
        return Quote(
            ticker=ticker,
            price=price,
            source=self.name,
            fetched_at=datetime.now(timezone.utc),
            bid=bid,
            ask=ask,
            volume=_safe_float(day.get('v')),
            raw=ticker_obj,
        )


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


# Self-register so QuoteMonitor consumers can do:
#   from src.ingestion.quote_sources import polygon, fmp, alpaca, yahoo
_registry.register('polygon', PolygonQuoteSource)
