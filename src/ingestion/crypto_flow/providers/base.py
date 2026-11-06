"""Provider contract, JSON fetch helper, and USD conversion for crypto-flow."""
from __future__ import annotations

import json
import math
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path

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


COINGECKO_URL = 'https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd'


def min_usd() -> float:
    return float(os.environ.get('OPENCLAW_CRYPTO_WHALE_MIN_USD', 1_000_000))


def resolve_prices(hints: dict, needed: dict, *, label: str, fetch=None) -> dict[str, float]:
    """ticker -> USD for `needed` ({ticker: Asset}): tick-driver hints first, then ONE CoinGecko call for the rest.
    `fetch` lets a provider route the call through its own (monkeypatchable) get_json."""
    prices = {t: p for t, p in (hints or {}).items() if t in needed and p}
    missing = {t: a for t, a in needed.items() if t not in prices and a.coingecko_id}
    if missing:
        headers = {'x-cg-demo-api-key': os.environ['COINGECKO_API_KEY']} if os.environ.get('COINGECKO_API_KEY') else None
        ids = ','.join(sorted({a.coingecko_id for a in missing.values()}))
        data = (fetch or get_json)(COINGECKO_URL.format(ids=ids), label='coingecko_simple_price', headers=headers)
        for t, a in missing.items():
            try:
                prices[t] = float(data[a.coingecko_id]['usd'])
            except (TypeError, KeyError, ValueError):
                pass
    print(f'[{label}] prices resolved={len(prices)} skipped_no_price={len(needed) - len(prices)}')
    return prices


class StateMixin:
    """JSON state file shared by all providers (atomic tmp+replace writes)."""
    state_path = Path('data/master/.crypto_flow_state.json')

    def load_state(self) -> dict:
        try:
            return json.loads(Path(self.state_path).read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)


def past_deadline(deadline_at: float | None) -> bool:
    """True once the tick's hard deadline (a time.monotonic() value set by the tick driver) has passed.
    Loop providers check this before each HTTP call and return partial results."""
    return deadline_at is not None and time.monotonic() >= deadline_at


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
