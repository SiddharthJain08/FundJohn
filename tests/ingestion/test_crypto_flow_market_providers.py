import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.ingestion.crypto_flow import universe
from src.ingestion.crypto_flow.models import MARKET_TICKER
from src.ingestion.crypto_flow.providers import cftc_cot, macro_sent, santiment, stables_defi
from src.ingestion.crypto_flow.providers.base import run_provider
from src.ingestion.crypto_flow.providers.cftc_cot import CftcCot
from src.ingestion.crypto_flow.providers.macro_sent import MacroSent
from src.ingestion.crypto_flow.providers.santiment import Santiment
from src.ingestion.crypto_flow.providers.stables_defi import StablesDefi

FX = Path(__file__).parent / 'fixtures' / 'crypto_flow'
NOW = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)
A = {a.ticker: a for a in universe.load_universe()}
SET = [A[t] for t in ('BTC-USD', 'ETH-USD', 'PEPE-USD', 'USDT-USD')]


def fx(name):
    return json.loads((FX / name).read_text())['payload']


class Router:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        for key, val in self.routes.items():
            if key in url:
                return val
        return None


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(santiment.time, 'sleep', lambda s: None)


def fields_of(res, ticker=MARKET_TICKER):
    return next(o.fields for o in res.observations if o.ticker == ticker)


def test_cot_picks_5btc_contract_and_nets(monkeypatch):
    r = Router({'gpe5-46if': fx('cftc_tff.json')})
    monkeypatch.setattr(cftc_cot, 'get_json', r)
    rows = fx('cftc_tff.json')
    btc = [x for x in rows if x['contract_market_name'] == 'BITCOIN']
    net = lambda x: int(x['lev_money_positions_long']) - int(x['lev_money_positions_short'])
    res = CftcCot().fetch([], NOW, NOW)
    f = fields_of(res)
    assert res.ok and f['cot_report_date'] == btc[0]['report_date_as_yyyy_mm_dd'][:10]
    assert f['cot_lev_net'] == net(btc[0]) and f['cot_lev_net_chg_1w'] == net(btc[0]) - net(btc[1])
    assert f['cot_am_net'] == int(btc[0]['asset_mgr_positions_long']) - int(btc[0]['asset_mgr_positions_short'])
    assert f['cot_dealer_net'] == int(btc[0]['dealer_positions_long_all']) - int(btc[0]['dealer_positions_short_all'])
    assert f['cot_oi'] == int(btc[0]['open_interest_all'])
    assert '%27%25BITCOIN%25%27' in r.calls[0][0] and ' ' not in r.calls[0][0]


def test_cot_none_is_error(monkeypatch):
    monkeypatch.setattr(cftc_cot, 'get_json', Router({}))
    assert not run_provider(CftcCot(), [], NOW, NOW).ok


def test_stables_sum_three_symbols(monkeypatch):
    monkeypatch.setattr(stables_defi, 'get_json', Router({
        'stablecoins.llama.fi': fx('llama_stablecoins.json'), 'v2/chains': fx('llama_chains.json'),
        'overview/dexs': fx('llama_dexs.json')}))
    res = StablesDefi().fetch([], NOW, NOW)
    peg = {a['symbol']: a['circulating']['peggedUSD'] for a in fx('llama_stablecoins.json')['peggedAssets']}
    m = fields_of(res)
    assert m['stable_supply_usd'] == pytest.approx(peg['USDT'] + peg['USDC'] + peg['USDG'])  # DAI excluded
    assert m['tvl_usd'] == pytest.approx(sum(c['tvl'] for c in fx('llama_chains.json')))
    assert m['dex_vol_24h_usd'] == fx('llama_dexs.json')['total24h']
    assert fields_of(res, 'USDC-USD') == {'circulating_usd': peg['USDC']}


def test_stables_secondary_none_gives_none_fields(monkeypatch):
    monkeypatch.setattr(stables_defi, 'get_json', Router({'stablecoins.llama.fi': fx('llama_stablecoins.json')}))
    m = fields_of(StablesDefi().fetch([], NOW, NOW))
    assert m['tvl_usd'] is None and m['dex_vol_24h_usd'] is None and m['stable_supply_usd'] > 0
    monkeypatch.setattr(stables_defi, 'get_json', Router({}))
    assert not run_provider(StablesDefi(), [], NOW, NOW).ok


def _macro_router(**over):
    routes = {'/global': fx('coingecko_global.json'), 'coins/markets': fx('coingecko_markets.json'),
              'fng': fx('fng.json'), 'coinpaprika': fx('coinpaprika_tickers.json')}
    routes.update(over)
    return Router(routes)


def test_macro_sent_header_when_key_set(monkeypatch):
    r = _macro_router()
    monkeypatch.setattr(macro_sent, 'get_json', r)
    monkeypatch.setenv('COINGECKO_API_KEY', 'k123')
    MacroSent().fetch(SET, NOW, NOW)
    cg = [kw for url, kw in r.calls if 'coingecko' in url]
    assert cg and all(kw['headers'] == {'x-cg-demo-api-key': 'k123'} for kw in cg)
    url = next(u for u, _ in r.calls if 'coins/markets' in u)
    assert 'ids=bitcoin,ethereum,pepe' in url and 'tether' not in url and 'price_change_percentage=1h,24h' in url
    monkeypatch.delenv('COINGECKO_API_KEY')
    r.calls.clear()
    MacroSent().fetch(SET, NOW, NOW)
    assert all(kw['headers'] is None for url, kw in r.calls if 'coingecko' in url)


def test_macro_sent_global_and_fng_fields(monkeypatch):
    monkeypatch.setattr(macro_sent, 'get_json', _macro_router())
    monkeypatch.delenv('COINGECKO_API_KEY', raising=False)
    res = MacroSent().fetch(SET, NOW, NOW)
    m = fields_of(res)
    assert m['btc_dominance'] == fx('coingecko_global.json')['data']['market_cap_percentage']['btc']
    assert m['total_mcap_usd'] == fx('coingecko_global.json')['data']['total_market_cap']['usd']
    assert m['fear_greed'] == int(fx('fng.json')['data'][0]['value'])
    btc = next(r for r in fx('coingecko_markets.json') if r['id'] == 'bitcoin')
    assert fields_of(res, 'BTC-USD') == {
        'price_usd': btc['current_price'], 'mcap_usd': btc['market_cap'], 'vol_24h_usd': btc['total_volume'],
        'ret_1h': btc['price_change_percentage_1h_in_currency'],
        'ret_24h': btc['price_change_percentage_24h_in_currency']}
    assert not any(o.ticker == 'USDT-USD' for o in res.observations)


def test_macro_sent_paprika_fallback(monkeypatch):
    monkeypatch.setattr(macro_sent, 'get_json', _macro_router(**{'coins/markets': None}))
    res = MacroSent().fetch(SET, NOW, NOW)
    eth = next(r for r in fx('coinpaprika_tickers.json') if r['symbol'] == 'ETH')['quotes']['USD']
    assert res.ok and fields_of(res, 'ETH-USD')['price_usd'] == eth['price']
    assert fields_of(res, 'ETH-USD')['ret_24h'] == eth['percent_change_24h']
    assert fields_of(res)['btc_dominance'] is not None


def test_macro_sent_both_down_is_error_and_fng_none(monkeypatch):
    monkeypatch.setattr(macro_sent, 'get_json', _macro_router(**{'coins/markets': None, 'coinpaprika': None, '/global': None}))
    assert not run_provider(MacroSent(), SET, NOW, NOW).ok
    monkeypatch.setattr(macro_sent, 'get_json', _macro_router(fng=None))
    assert fields_of(MacroSent().fetch(SET, NOW, NOW))['fear_greed'] is None


def test_santiment_disabled_without_key(monkeypatch):
    monkeypatch.delenv('SANTIMENT_API_KEY', raising=False)
    res = Santiment().fetch([], NOW, NOW)
    assert res.skipped and res.ok and not res.observations


def test_santiment_rows_keyed_ticker_date_metric(monkeypatch, tmp_path):
    r = Router({'santiment': fx('santiment_exchange_balance.json')})
    monkeypatch.setattr(santiment, 'get_json', r)
    monkeypatch.setenv('SANTIMENT_API_KEY', 'sekret')
    p = Santiment()
    p.onchain_daily_path = tmp_path / 'onchain.parquet'
    res = p.fetch([], NOW, NOW)
    df = pd.read_parquet(p.onchain_daily_path)
    series = fx('santiment_exchange_balance.json')['data']['getMetric']['timeseriesData']
    btc = df[(df.ticker == 'BTC-USD') & (df.metric == 'exchange_balance')].sort_values('date')
    assert list(btc['date']) == [x['datetime'][:10] for x in series]
    assert list(btc['value']) == [x['value'] for x in series] and set(df.source) == {'santiment'}
    assert len(r.calls) == 10 and r.calls[0][1]['headers'] == {'Authorization': 'Apikey sekret'}
    assert 'sekret' not in r.calls[0][0]
    f = fields_of(res)
    assert f['santiment_rows_written'] == len(df) == 30 and f['santiment_latest_date'] == series[-1]['datetime'][:10]
    assert f['san_btc_exchange_balance_30d_lag'] == series[-1]['value']


def test_santiment_restricted_error_is_error_not_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(santiment, 'get_json', Router({'santiment': fx('santiment_error.json')}))
    monkeypatch.setenv('SANTIMENT_API_KEY', 'sekret')
    p = Santiment()
    p.onchain_daily_path = tmp_path / 'onchain.parquet'
    res = run_provider(p, [], NOW, NOW)
    assert not res.ok and res.err and 'sekret' not in res.err and not p.onchain_daily_path.exists()


def test_santiment_partial_failure_counts(monkeypatch, tmp_path):
    seq = iter([fx('santiment_error.json')] + [fx('santiment_exchange_balance.json')] * 9)
    monkeypatch.setattr(santiment, 'get_json', lambda *a, **k: next(seq))
    monkeypatch.setenv('SANTIMENT_API_KEY', 'sekret')
    p = Santiment()
    p.onchain_daily_path = tmp_path / 'onchain.parquet'
    f = fields_of(p.fetch([], NOW, NOW))
    assert f['santiment_queries_failed'] == 1 and f['santiment_rows_written'] == 27
