"""Alpaca adapter — wraps data.alpaca.markets/v2/stocks/{T}/snapshot.

Upstream call shape mirrored from:
  src/execution/alpaca_executor.py:485-526
    GET https://data.alpaca.markets/v2/stocks/{ticker}/snapshot
    headers: APCA-API-KEY-ID, APCA-API-SECRET-KEY

paper-api.alpaca.markets does NOT serve market data; we must hit
data.alpaca.markets explicitly (same gotcha the executor already
documents in its own code comment).

Prefer latestTrade.p (matches what Alpaca's own order endpoint validates
against) over the quote midpoint, which can be miles off in wide markets.
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


class AlpacaQuoteSource:
    name = 'alpaca'
    priority = 2  # third behind Polygon + FMP

    BASE = 'https://data.alpaca.markets'

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        timeout: int = 5,
    ):
        self.api_key    = api_key    or os.environ.get('ALPACA_API_KEY', '')
        self.api_secret = api_secret or os.environ.get('ALPACA_SECRET_KEY',
                                       os.environ.get('ALPACA_API_SECRET', ''))
        self.timeout = timeout

    async def fetch(self, tickers: Iterable[str]) -> dict[str, Quote]:
        if not (self.api_key and self.api_secret):
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
        req = urllib.request.Request(
            f'{self.BASE}/v2/stocks/{ticker}/snapshot',
            headers={
                'APCA-API-KEY-ID':     self.api_key,
                'APCA-API-SECRET-KEY': self.api_secret,
            },
        )
        body = fetch_with_retry(req, timeout=self.timeout, label=f'alpaca-snap-{ticker}')
        if body is None:
            return None
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return None
        latest_trade = (data or {}).get('latestTrade') or {}
        latest_quote = (data or {}).get('latestQuote') or {}
        daily_bar    = (data or {}).get('dailyBar')    or {}
        trade_p = _safe_float(latest_trade.get('p'))
        bid     = _safe_float(latest_quote.get('bp'))
        ask     = _safe_float(latest_quote.get('ap'))
        price = trade_p
        if price is None:
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                price = (bid + ask) / 2.0
            else:
                price = bid or ask
        if price is None or price <= 0:
            return None
        return Quote(
            ticker=ticker,
            price=price,
            source=self.name,
            fetched_at=datetime.now(timezone.utc),
            bid=bid,
            ask=ask,
            volume=_safe_float(daily_bar.get('v')),
            raw=data,
        )


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


_registry.register('alpaca', AlpacaQuoteSource)
