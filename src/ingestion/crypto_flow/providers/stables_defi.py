"""stables_defi: stablecoin supply, DeFi TVL and DEX volume from DefiLlama (market-wide)."""
from __future__ import annotations

from src.ingestion.crypto_flow.models import MARKET_TICKER, Observation
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json

STABLES_URL = 'https://stablecoins.llama.fi/stablecoins?includePrices=false'
CHAINS_URL = 'https://api.llama.fi/v2/chains'
DEXS_URL = 'https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true'
SYMBOLS = ('USDT', 'USDC', 'USDG')


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def circulating_by_symbol(payload) -> dict[str, float]:
    """symbol -> circulating USD, largest entry per symbol (llama lists several assets sharing a ticker)."""
    out: dict[str, float] = {}
    for a in (payload.get('peggedAssets') or []) if isinstance(payload, dict) else []:
        sym = a.get('symbol') if isinstance(a, dict) else None
        v = _f((a.get('circulating') or {}).get('peggedUSD')) if sym in SYMBOLS else None
        if v is not None and v > out.get(sym, -1.0):
            out[sym] = v
    return out


class StablesDefi(Provider):
    name = 'stables_defi'
    cadence = 'hourly'

    def _fetch(self, assets, since, now):
        circ = circulating_by_symbol(get_json(STABLES_URL, label='llama_stablecoins', timeout=15))
        if not circ:
            raise ProviderFetchError('stables_defi: stablecoins None')
        chains = get_json(CHAINS_URL, label='llama_chains', timeout=15)
        tvls = [_f(c.get('tvl')) for c in chains if isinstance(c, dict)] if isinstance(chains, list) else []
        tvls = [t for t in tvls if t is not None]
        dexs = get_json(DEXS_URL, label='llama_dexs', timeout=15)
        market = {'stable_supply_usd': sum(circ.values()),
                  'tvl_usd': sum(tvls) if tvls else None,
                  'dex_vol_24h_usd': _f(dexs.get('total24h')) if isinstance(dexs, dict) else None}
        obs = [Observation(now, MARKET_TICKER, self.name, market)]
        obs += [Observation(now, f'{s}-USD', self.name, {'circulating_usd': v}) for s, v in circ.items()]
        return [], obs
