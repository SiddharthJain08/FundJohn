"""santiment: 30-day-lagged daily on-chain history (exchange balance/flows, whales, DAA) for BTC/ETH.

Unlike its siblings this provider WRITES its own parquet (data/master/crypto_onchain_daily.parquet, key
(ticker, date, metric)) instead of returning events: the free tier serves only data older than 30 days, so
this is lagged daily history, not a live signal, and does not belong in the hourly flow tables. Santiment
revises, hence write_onchain_daily's replace mode. The API key travels in a header, never the URL/errors."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pandas as pd

from src.ingestion.crypto_flow import store
from src.ingestion.crypto_flow.models import MARKET_TICKER, Observation
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json

URL = 'https://api.santiment.net/graphql'
METRICS = ('exchange_balance', 'exchange_inflow', 'exchange_outflow',
           'whale_transaction_count_1m_usd_to_inf', 'daily_active_addresses')
SLUGS = {'bitcoin': 'BTC-USD', 'ethereum': 'ETH-USD'}
QUERY = ('{ getMetric(metric:"%s"){ timeseriesData(slug:"%s", from:"utc_now-45d", to:"utc_now-31d", '
         'interval:"1d"){ datetime value } } }')


def parse_timeseries(payload) -> list[tuple[str, float]] | None:
    """[(date, value)] or None when the payload is missing, carries GraphQL errors[] or has no series."""
    if not isinstance(payload, dict) or payload.get('errors'):
        return None
    try:
        series = payload['data']['getMetric']['timeseriesData']
        return [(p['datetime'][:10], float(p['value'])) for p in series if p.get('value') is not None]
    except (TypeError, KeyError, ValueError):
        return None


class Santiment(Provider):
    name = 'santiment'
    cadence = 'daily'
    onchain_daily_path = store.ONCHAIN_DAILY_PATH

    def enabled(self):
        if not os.environ.get('SANTIMENT_API_KEY'):
            return False, 'SANTIMENT_API_KEY not set'
        return True, None

    def _fetch(self, assets, since, now):
        headers = {'Authorization': f"Apikey {os.environ['SANTIMENT_API_KEY']}"}
        fetched_at, recs, failed = pd.Timestamp(now), [], 0
        for metric in METRICS:
            for slug, ticker in SLUGS.items():
                series = parse_timeseries(get_json(URL, label=f'santiment_{metric}_{slug}', timeout=15,
                                                   headers=headers, method='POST',
                                                   body={'query': QUERY % (metric, slug)}))
                time.sleep(0.2)
                if series is None:
                    failed += 1
                    continue
                recs += [(ticker, d, metric, v, 'santiment', fetched_at) for d, v in series]
        if failed == len(METRICS) * len(SLUGS):
            raise ProviderFetchError('santiment: all queries failed or returned GraphQL errors')
        df = pd.DataFrame(recs, columns=['ticker', 'date', 'metric', 'value', 'source', 'fetched_at'])
        if not df.empty:
            store.write_onchain_daily(df, path=self.onchain_daily_path)
        fields = {'santiment_rows_written': len(df), 'santiment_queries_failed': failed,
                  'santiment_latest_date': df['date'].max() if not df.empty else None}
        for ticker, key in (('BTC-USD', 'san_btc_exchange_balance_30d_lag'),
                            ('ETH-USD', 'san_eth_exchange_balance_30d_lag')):
            sub = df[(df.ticker == ticker) & (df.metric == 'exchange_balance')].sort_values('date')
            fields[key] = float(sub['value'].iloc[-1]) if not sub.empty else None
        return [], [Observation(now, MARKET_TICKER, self.name, fields)]
