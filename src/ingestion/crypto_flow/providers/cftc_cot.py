"""cftc_cot: CFTC Traders-in-Financial-Futures positioning for the CME 5-BTC contract (weekly, Socrata)."""
from __future__ import annotations

from urllib.parse import quote

from src.ingestion.crypto_flow.models import MARKET_TICKER, Observation
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json

URL = ('https://publicreporting.cftc.gov/resource/gpe5-46if.json?$where='
       + quote("contract_market_name like '%BITCOIN%'") + '&$order=' + quote('report_date_as_yyyy_mm_dd DESC')
       + '&$limit=12')
CONTRACT = 'BITCOIN'  # the CME 5-BTC contract; NOT 'MICRO BITCOIN' / 'NANO BITCOIN PERP STYLE' / 'BITCOIN CASH ...'


def _i(x) -> int | None:
    """Socrata serialises numbers as strings."""
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _net(row, long_k: str, short_k: str) -> int | None:
    a, b = _i(row.get(long_k)), _i(row.get(short_k))
    return a - b if a is not None and b is not None else None


class CftcCot(Provider):
    name = 'cftc_cot'
    cadence = 'daily'

    def _fetch(self, assets, since, now):
        data = get_json(URL, label='cftc_cot', timeout=15)
        rows = [r for r in data if isinstance(r, dict) and r.get('contract_market_name') == CONTRACT] \
            if isinstance(data, list) else []
        if not rows:
            raise ProviderFetchError('cftc_cot: no BITCOIN rows')
        cur = rows[0]  # query is ordered newest first
        lev = _net(cur, 'lev_money_positions_long', 'lev_money_positions_short')
        prev = _net(rows[1], 'lev_money_positions_long', 'lev_money_positions_short') if len(rows) > 1 else None
        fields = {
            'cot_report_date': str(cur.get('report_date_as_yyyy_mm_dd', ''))[:10] or None,
            'cot_lev_net': lev,
            'cot_am_net': _net(cur, 'asset_mgr_positions_long', 'asset_mgr_positions_short'),
            'cot_dealer_net': _net(cur, 'dealer_positions_long_all', 'dealer_positions_short_all'),
            'cot_oi': _i(cur.get('open_interest_all')),
            'cot_lev_net_chg_1w': lev - prev if lev is not None and prev is not None else None,
        }
        return [], [Observation(now, MARKET_TICKER, self.name, fields)]
