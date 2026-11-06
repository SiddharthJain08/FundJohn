"""hl_whales: aggregate perp positioning of the top-N Hyperliquid accounts by account value."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from src.ingestion.crypto_flow import universe
from src.ingestion.crypto_flow.models import Observation
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json

LEADERBOARD_URL = 'https://stats-data.hyperliquid.xyz/Mainnet/leaderboard'
INFO_URL = 'https://api.hyperliquid.xyz/info'
LEADERBOARD_TTL = 24 * 3600


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def top_n() -> int:
    return int(os.environ.get('OPENCLAW_CRYPTO_HL_WHALES_TOP_N', 200))


def parse_leaderboard(payload, top_n: int) -> list[str]:
    """Addresses of the `top_n` largest accounts by accountValue; [] on a malformed payload."""
    rows = payload.get('leaderboardRows') if isinstance(payload, dict) else None
    ranked = []
    for r in rows or []:
        v = _f(r.get('accountValue')) if isinstance(r, dict) else None
        if v is not None and r.get('ethAddress'):
            ranked.append((v, r['ethAddress']))
    return [a for _, a in sorted(ranked, reverse=True)[:top_n]]


def aggregate_positions(states: list[dict], coin_to_ticker: dict) -> dict[str, dict]:
    """ticker -> long/short/net USD and counts. szi sign is the direction; positionValue is unsigned.
    Coins outside the universe are dropped."""
    out: dict[str, dict] = {}
    for st in states:
        for ap in (st or {}).get('assetPositions') or []:
            pos = ap.get('position') or {}
            ticker = coin_to_ticker.get(pos.get('coin'))
            szi, val = _f(pos.get('szi')), _f(pos.get('positionValue'))
            if ticker is None or not szi or val is None:
                continue
            rec = out.setdefault(ticker, {'whale_perp_net_usd': 0.0, 'whale_perp_long_usd': 0.0,
                                          'whale_perp_short_usd': 0.0, 'whale_perp_n_long': 0,
                                          'whale_perp_n_short': 0})
            side = 'long' if szi > 0 else 'short'
            rec[f'whale_perp_{side}_usd'] += val
            rec[f'whale_perp_n_{side}'] += 1
            rec['whale_perp_net_usd'] += val if szi > 0 else -val
    return out


class HlWhales(Provider):
    name = 'hl_whales'
    cadence = 'hourly'
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

    def _addresses(self, n: int) -> list[str]:
        """Top-n addresses; cached in state for 24 h, and a stale cache is the fallback if the refetch fails."""
        state = self.load_state()
        cache = state.get('hl_whales_leaderboard') or {}
        try:
            age = time.time() - datetime.fromisoformat(cache['fetched_at']).timestamp()
        except (KeyError, TypeError, ValueError):
            age = None
        if age is not None and age < LEADERBOARD_TTL and cache.get('addresses'):
            return cache['addresses'][:n]
        addrs = parse_leaderboard(get_json(LEADERBOARD_URL, label='hl_leaderboard', timeout=60), n)
        if not addrs:
            if cache.get('addresses'):  # stale cache beats no data
                hours = f'{age / 3600:.1f}' if age is not None else '?'
                print(f'[hl_whales] leaderboard refetch failed; using cached list age={hours}h')
                return cache['addresses'][:n]
            raise ProviderFetchError('hl_whales: leaderboard None')
        self.save_state({**state, 'hl_whales_leaderboard': {
            'fetched_at': datetime.fromtimestamp(time.time(), timezone.utc).isoformat(), 'addresses': addrs}})
        return addrs

    def _fetch(self, assets, since, now):
        addrs = self._addresses(top_n())
        states, failed = [], 0
        for i, addr in enumerate(addrs):
            if i:
                time.sleep(0.1)
            st = get_json(INFO_URL, label='hl_clearinghouse', timeout=15, method='POST',
                          body={'type': 'clearinghouseState', 'user': addr})
            if isinstance(st, dict):
                states.append(st)
                continue
            failed += 1
            if failed * 2 > len(addrs):
                raise ProviderFetchError(f'hl_whales: {failed}/{len(addrs)} accounts failed')
        coin_to_ticker = {a.hyperliquid: a.ticker for a in universe.load_universe() if a.hyperliquid}
        agg = aggregate_positions(states, coin_to_ticker)
        return [], [Observation(now, t, self.name, rec | {'whale_perp_accounts_seen': len(states)})
                    for t, rec in agg.items()]
