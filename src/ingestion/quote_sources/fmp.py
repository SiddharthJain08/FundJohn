"""FMP (Financial Modeling Prep) adapter — wraps /stable/quote.

Upstream call shape mirrored from:
  src/agent/tools/mcp/fmp.js -> get_quote
  src/ingestion/pipeline.py  -> FMP_BASE pattern (apikey query param)

FMP free-tier returns 402 on overrun; we treat that the same as any other
upstream failure and fall back to other sources via the orchestrator.
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


class FMPQuoteSource:
    name = 'fmp'
    priority = 2  # P2 in production chain: Alpaca P1, FMP P2 (Polygon removed in Task 17)

    BASE = 'https://financialmodelingprep.com/stable'

    def __init__(self, api_key: str | None = None, timeout: int = 5):
        # See PolygonQuoteSource for the None-vs-'' rationale.
        self.api_key = api_key if api_key is not None else os.environ.get('FMP_API_KEY', '')
        self.timeout = timeout

    async def fetch(self, tickers: Iterable[str]) -> dict[str, Quote]:
        if not self.api_key:
            return {}
        ticker_list = list(tickers)
        results = await asyncio.gather(
            *(asyncio.to_thread(self._fetch_one, t) for t in ticker_list),
            return_exceptions=True,
        )
        out: dict[str, Quote] = {}
        for t, r in zip(ticker_list, results):
            if isinstance(r, Quote):
                out[t] = r
        return out

    def _fetch_one(self, ticker: str) -> Quote | None:
        qs = urllib.parse.urlencode({'symbol': ticker, 'apikey': self.api_key})
        req = urllib.request.Request(
            f'{self.BASE}/quote?{qs}',
            headers={'User-Agent': 'OpenClaw-FundJohn/1.0'},
        )
        body = fetch_with_retry(req, timeout=self.timeout, label=f'fmp-quote-{ticker}')
        if body is None:
            return None
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return None
        # FMP returns a list with one element for /quote?symbol=X
        if isinstance(data, list):
            row = data[0] if data else None
        else:
            row = data
        if not isinstance(row, dict):
            return None
        price = _safe_float(row.get('price'))
        if price is None or price <= 0:
            return None
        return Quote(
            ticker=ticker,
            price=price,
            source=self.name,
            fetched_at=datetime.now(timezone.utc),
            bid=_safe_float(row.get('bid')),
            ask=_safe_float(row.get('ask')),
            volume=_safe_float(row.get('volume')),
            raw=row,
        )


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


_registry.register('fmp', FMPQuoteSource)
