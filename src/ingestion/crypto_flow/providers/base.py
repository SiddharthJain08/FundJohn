"""Provider contract, JSON fetch helper, and USD conversion for crypto-flow."""
from __future__ import annotations

import json
import math
import time
import urllib.request
from datetime import datetime

from src.ingestion._http_retry import fetch_with_retry
from src.ingestion.crypto_flow.models import Observation, ProviderResult, WhaleEvent
from src.ingestion.crypto_flow.universe import Asset

_UA = 'openclaw-crypto-flow/1.0'


def get_json(url: str, *, label: str, timeout: int = 20, headers: dict | None = None,
             method: str = 'GET', body: dict | None = None):
    """Fetch and decode JSON. None on fetch failure, empty or non-JSON body."""
    h = {'User-Agent': _UA, 'Accept': 'application/json'}
    data = None
    if body is not None:
        h['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    raw = fetch_with_retry(req, timeout=timeout, attempts=2, label=label)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def usd(amount: float, price: float | None) -> float | None:
    if price is None or not math.isfinite(price):
        return None
    return amount * price


class ProviderFetchError(RuntimeError):
    """Raised by a provider when its PRIMARY source returns None/garbage so run_provider records ok=False."""


class Provider:
    """Subclasses implement _fetch. If the primary source returns None/garbage, raise
    ProviderFetchError; fallback sources may swallow None."""
    name: str = ''
    cadence: str = 'tick'  # 'tick' | 'hourly' | 'daily'

    def enabled(self) -> tuple[bool, str | None]:
        return True, None

    def fetch(self, assets: list[Asset], since: datetime, now: datetime) -> ProviderResult:
        t0 = time.monotonic()
        ok, reason = self.enabled()
        if not ok:
            return ProviderResult(self.name, True, [], [], 0, int((time.monotonic() - t0) * 1000), skipped=reason)
        events, obs = self._fetch(assets, since, now)
        return ProviderResult(self.name, True, events, obs, len(events) + len(obs),
                              int((time.monotonic() - t0) * 1000))

    def _fetch(self, assets: list[Asset], since: datetime, now: datetime
               ) -> tuple[list[WhaleEvent], list[Observation]]:
        raise NotImplementedError


def run_provider(p: Provider, assets, since, now) -> ProviderResult:
    """Run a provider; never raises."""
    t0 = time.monotonic()
    try:
        return p.fetch(assets, since, now)
    except Exception as e:
        return ProviderResult(p.name, False, [], [], 0, int((time.monotonic() - t0) * 1000),
                              err=f'{type(e).__name__}: {e}')
