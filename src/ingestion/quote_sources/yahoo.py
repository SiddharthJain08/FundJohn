"""Yahoo Tier-2 adapter — wraps yfinance.Ticker(...).fast_info.

Upstream call shape mirrored from:
  src/pipeline/collector.js (embedded python script around line 720)
    fi = t.fast_info
    S = f(getattr(fi, 'last_price', None)
          or getattr(fi, 'regularMarketPrice', None))

This is the only adapter that does NOT go through urllib + _http_retry —
yfinance owns its own scraping path and we honor that. Wrapped in
asyncio.to_thread so the orchestrator's per-source ack timeout still
applies.

Import-safe: yfinance is imported lazily inside _fetch_one so that
collecting this module (e.g. for the SOURCE_REGISTRY hookup) doesn't
require yfinance to be installed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterable

from src.ingestion import quote_sources as _registry
from src.ingestion.quote_monitor import Quote


class YahooQuoteSource:
    name = 'yahoo'
    priority = 3  # Tier-2 fallback per memory note (Polygon→FMP→Alpaca→Yahoo)

    def __init__(self, timeout: int = 5):
        # yfinance doesn't expose a per-call timeout (it uses requests
        # internally with its own defaults). The orchestrator's
        # asyncio.wait_for(ack_timeout_sec=3.0) is the real fence.
        self.timeout = timeout

    async def fetch(self, tickers: Iterable[str]) -> dict[str, Quote]:
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
        try:
            import yfinance as yf  # lazy import — see module docstring
        except ImportError:
            return None
        try:
            t = yf.Ticker(ticker)
            price = None
            try:
                fi = t.fast_info
                price = _safe_float(
                    getattr(fi, 'last_price', None)
                    or getattr(fi, 'regularMarketPrice', None),
                )
            except Exception:
                pass
            if price is None:
                # Fall-through to the same history(period='1d') path the
                # collector.js embedded script uses.
                try:
                    hist = t.history(period='1d')
                    if not hist.empty:
                        price = _safe_float(hist['Close'].iloc[-1])
                except Exception:
                    pass
            if price is None or price <= 0:
                return None
            return Quote(
                ticker=ticker,
                price=price,
                source=self.name,
                fetched_at=datetime.now(timezone.utc),
                bid=None,
                ask=None,
                volume=None,
                raw=None,
            )
        except Exception:
            return None


def _safe_float(v) -> float | None:
    try:
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


_registry.register('yahoo', YahooQuoteSource)
