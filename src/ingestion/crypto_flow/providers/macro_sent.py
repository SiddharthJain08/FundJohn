"""macro_sent: BTC dominance / total mcap, per-asset price+mcap+volume+returns, and Fear & Greed.

Per-asset outputs are named spot_px / spot_mcap_usd / spot_vol_24h_usd / cg_ret_1h / cg_ret_24h so they never
collide with the feature OUTPUTS price_usd / ret_1h / ret_24h (nor derivs' vol_24h_usd)."""
from __future__ import annotations

import os
from urllib.parse import quote

from src.ingestion.crypto_flow.models import MARKET_TICKER, Observation
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json

CG = 'https://api.coingecko.com/api/v3'
PAPRIKA_URL = 'https://api.coinpaprika.com/v1/tickers'
FNG_URL = 'https://api.alternative.me/fng/?limit=1'


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _get(d, *path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


class MacroSent(Provider):
    name = 'macro_sent'
    cadence = 'hourly'

    def _per_asset_coingecko(self, assets, headers):
        ids = ','.join(sorted({a.coingecko_id for a in assets if a.coingecko_id and a.role != 'stable'}))
        data = get_json(f'{CG}/coins/markets?vs_currency=usd&ids={quote(ids, safe=",")}'
                        '&price_change_percentage=1h,24h', label='coingecko_markets', timeout=15, headers=headers)
        if not isinstance(data, list):
            return None
        by_id = {r.get('id'): r for r in data if isinstance(r, dict)}
        out = {}
        for a in assets:
            r = by_id.get(a.coingecko_id)
            if r:
                r24 = r.get('price_change_percentage_24h_in_currency')
                out[a.ticker] = {'spot_px': _f(r.get('current_price')), 'spot_mcap_usd': _f(r.get('market_cap')),
                                 'spot_vol_24h_usd': _f(r.get('total_volume')),
                                 'cg_ret_1h': _f(r.get('price_change_percentage_1h_in_currency')),
                                 'cg_ret_24h': _f(r24 if r24 is not None else r.get('price_change_percentage_24h'))}
        return out

    def _per_asset_paprika(self, assets):
        data = get_json(PAPRIKA_URL, label='coinpaprika_tickers', timeout=15)
        if not isinstance(data, list):
            return None
        by_sym: dict[str, list[dict]] = {}
        for r in data:
            if isinstance(r, dict):
                by_sym.setdefault(str(r.get('symbol', '')).upper(), []).append(r)
        out, self.paprika_ambiguous = {}, 0
        for a in assets:
            cands = by_sym.get(a.ticker.split('-')[0].upper(), [])
            cg = a.coingecko_id or ''
            # symbols collide (SKY, TRUMP, RENDER...): exact id suffix (`btc-bitcoin`), else a lone ranked candidate
            row = next((r for r in cands if cg and str(r.get('id', '')).endswith('-' + cg)), None)
            if row is None and len(cands) == 1 and (_f(cands[0].get('rank')) or 0) > 0:
                row = cands[0]
            if row is None and cands:
                self.paprika_ambiguous += 1
            q = _get(row, 'quotes', 'USD')
            if q:
                out[a.ticker] = {'spot_px': _f(q.get('price')), 'spot_mcap_usd': _f(q.get('market_cap')),
                                 'spot_vol_24h_usd': _f(q.get('volume_24h')), 'cg_ret_1h': _f(q.get('percent_change_1h')),
                                 'cg_ret_24h': _f(q.get('percent_change_24h'))}
        return out

    def _fetch(self, assets, since, now):
        self.paprika_ambiguous = 0
        key = os.environ.get('COINGECKO_API_KEY')
        headers = {'x-cg-demo-api-key': key} if key else None
        glob = get_json(f'{CG}/global', label='coingecko_global', timeout=15, headers=headers)
        per_asset = self._per_asset_coingecko([a for a in assets if a.role != 'stable'], headers)
        if per_asset is None:
            per_asset = self._per_asset_paprika([a for a in assets if a.role != 'stable'])
        if per_asset is None:  # per-asset prices are the primary output; /global is supplementary
            raise ProviderFetchError('macro_sent: coingecko and coinpaprika both None')
        fng = get_json(FNG_URL, label='alternative_fng', timeout=15)
        try:
            fear_greed = int(fng['data'][0]['value'])
        except (TypeError, KeyError, IndexError, ValueError):
            fear_greed = None
        market = {'btc_dominance': _f(_get(glob, 'data', 'market_cap_percentage', 'btc')),
                  'total_mcap_usd': _f(_get(glob, 'data', 'total_market_cap', 'usd')), 'fear_greed': fear_greed,
                  'paprika_ambiguous': self.paprika_ambiguous or None}
        return [], [Observation(now, MARKET_TICKER, self.name, market)] + [
            Observation(now, t, self.name, f) for t, f in (per_asset or {}).items()]
