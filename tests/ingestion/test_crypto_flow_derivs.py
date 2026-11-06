import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingestion.crypto_flow import universe
from src.ingestion.crypto_flow.models import MARKET_TICKER
from src.ingestion.crypto_flow.providers import base, derivs, liquidations
from src.ingestion.crypto_flow.providers.derivs import Derivs, _unit_multiplier, parse_hl, select_top
from src.ingestion.crypto_flow.providers.liquidations import Liquidations, parse_okx_liqs

FX = Path(__file__).parent / 'fixtures' / 'crypto_flow'
NOW = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)
ALL = universe.load_universe()
A = {a.ticker: a for a in ALL}
SET = [A[t] for t in ('BTC-USD', 'ETH-USD', 'PEPE-USD', 'BAT-USD', 'USDT-USD')]


def fx(name):
    return json.loads((FX / name).read_text())['payload']


class Router:
    """Route get_json by URL substring; unmatched or None => None (HTML error / outage)."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, **kw):
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                return val
        return None


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(derivs.time, 'sleep', lambda s: None)


def patch(monkeypatch, routes):
    r = Router(routes)
    monkeypatch.setattr(derivs, 'get_json', r)
    monkeypatch.setattr(liquidations, 'get_json', r)
    return r


HL = {'api.hyperliquid.xyz': fx('hl_meta_and_ctxs.json')}
BIN = {'fapi/v1/openInterest': fx('binance_oi.json'), 'premiumIndex': fx('binance_premium.json'),
       'globalLongShort': fx('binance_global_ls.json'), 'topLongShort': fx('binance_top_ls.json'),
       'takerlongshort': fx('binance_taker.json')}


def by_ticker(res):
    return {o.ticker: o.fields for o in res.observations}


def test_unit_multiplier_k_and_1000_symbols():
    assert [_unit_multiplier(s) for s in ('kBONK', 'kPEPE', 'kSHIB', '1000BONKUSDT', '1000PEPEUSDT',
                                          'SHIB1000USDT')] == [1000] * 6
    assert [_unit_multiplier(s) for s in ('BTC', 'HYPE', 'BTCUSDT', 'KAVA', None)] == [1] * 5


def test_hyperliquid_primary_fills_oi_funding_for_all_mapped():
    out = parse_hl(fx('hl_meta_and_ctxs.json'), SET)
    assert set(out) == {'BTC-USD', 'ETH-USD', 'PEPE-USD'}  # BAT has no HL listing; USDT is not in fixture
    btc = out['BTC-USD']
    assert btc['oi_usd'] == pytest.approx(42343.94552 * 75817.0)
    assert btc['funding_8h'] == pytest.approx(0.0000125 * 8)
    assert btc['premium'] == pytest.approx(-0.0000580273) and btc['vol_24h_usd'] > 1e9
    pepe_raw = next(c for m, c in zip(fx('hl_meta_and_ctxs.json')[0]['universe'],
                                      fx('hl_meta_and_ctxs.json')[1]) if m['name'] == 'kPEPE')
    assert out['PEPE-USD']['mark_px'] == pytest.approx(float(pepe_raw['markPx']) / 1000)
    assert out['PEPE-USD']['oi_usd'] == pytest.approx(float(pepe_raw['openInterest']) * float(pepe_raw['markPx']))


def test_binance_fallback_only_for_ratios(monkeypatch):
    patch(monkeypatch, {**HL, **BIN})
    res = Derivs().fetch(SET, NOW, NOW)
    assert res.ok and res.err is None
    f = by_ticker(res)['BTC-USD']
    assert f['venues'] == 'hyperliquid,binance'
    assert f['ls_ratio_global'] == pytest.approx(3.1034) and f['ls_ratio_top'] == pytest.approx(2.0569)
    assert f['taker_buy_sell'] == pytest.approx(0.8424)
    assert f['oi_usd'] == pytest.approx(42343.94552 * 75817.0)  # HL stays the OI source
    assert f['px_source'] == 'hyperliquid'


def test_oi_total_requires_two_venues(monkeypatch):
    patch(monkeypatch, {**HL, **BIN})
    f = by_ticker(Derivs().fetch([A['BTC-USD']], NOW, NOW))['BTC-USD']
    assert f['oi_usd_total'] == pytest.approx(
        42343.94552 * 75817.0 + 96576.544 * float(fx('binance_premium.json')['markPrice']))
    assert f['oi_venues'] == 'hyperliquid,binance'
    patch(monkeypatch, {**HL})  # HL alone (Binance down) => one venue => no total
    f = by_ticker(Derivs().fetch([A['BTC-USD']], NOW, NOW))['BTC-USD']
    assert f['oi_usd_total'] is None and f['oi_venues'] == 'hyperliquid' and f['venues'] == 'hyperliquid'


def test_binance_html_error_is_provider_degraded_not_crash(monkeypatch):
    r = patch(monkeypatch, {**HL})  # every other URL -> None
    res = Derivs().fetch(SET, NOW, NOW)
    assert res.ok and res.err is None
    f = by_ticker(res)
    assert f['BTC-USD']['ls_ratio_global'] is None and f['BTC-USD']['oi_usd'] > 0
    assert f[MARKET_TICKER] == {'binance_degraded': True}
    assert sum('fapi.binance.com' in u for u in r.calls) == 1  # gave up after the first call
    assert 'USDT-USD' not in f


def test_bybit_ratio_fallback_when_binance_degraded(monkeypatch):
    patch(monkeypatch, {**HL, 'account-ratio': fx('bybit_account_ratio.json'),
                        'v5/market/open-interest': fx('bybit_oi.json')})
    f = by_ticker(Derivs().fetch([A['BTC-USD']], NOW, NOW))['BTC-USD']
    assert f['ls_ratio_global'] == pytest.approx(0.7113 / 0.2887)
    assert f['venues'] == 'hyperliquid,bybit' and f['oi_usd_total'] is not None


def test_hyperliquid_none_raises(monkeypatch):
    patch(monkeypatch, {})
    res = base.run_provider(Derivs(), SET, NOW, NOW)
    assert not res.ok and 'hyperliquid None' in res.err


def test_binance_capped_to_top_n_always_includes_btc_eth(monkeypatch):
    vols = {a.ticker: float(i) for i, a in enumerate(ALL)}  # later universe rows = bigger volume
    got = [a.ticker for a in select_top(ALL, vols, 'binance', 5)]
    assert len(got) == 5 and {'BTC-USD', 'ETH-USD'} <= set(got)
    assert not {'USDC-USD', 'USDT-USD'} & set(got)
    assert got[2:] == ['YFI-USD', 'XTZ-USD', 'XRP-USD']  # highest-volume remainder
    monkeypatch.setenv('OPENCLAW_CRYPTO_DERIVS_BINANCE_TOP_N', '2')
    r = patch(monkeypatch, {**HL, **BIN})
    Derivs().fetch(ALL, NOW, NOW)
    assert sum('premiumIndex' in u for u in r.calls) == 2


def test_okx_liquidations_split_by_side():
    payload = fx('okx_liq.json')
    ctval = {'BTC-USDT-SWAP': 0.01}
    rows = payload['data'][0]['details']
    now = datetime.fromtimestamp(int(rows[0]['ts']) / 1000 + 60, tz=timezone.utc)  # window = whole capture
    got = parse_okx_liqs(payload, ctval, now)['BTC-USDT-SWAP']
    exp_long = sum(float(d['sz']) * 0.01 * float(d['bkPx']) for d in rows if d['posSide'] == 'long')
    exp_short = sum(float(d['sz']) * 0.01 * float(d['bkPx']) for d in rows if d['posSide'] == 'short')
    assert got['liq_long_usd_1h'] == pytest.approx(exp_long) and got['liq_short_usd_1h'] == pytest.approx(exp_short)
    assert got['liq_n_1h'] == 12 and exp_long > exp_short > 0
    late = datetime.fromtimestamp(int(rows[0]['ts']) / 1000 + 3600 + 1, tz=timezone.utc)  # all stale
    assert parse_okx_liqs(payload, ctval, late)['BTC-USDT-SWAP']['liq_n_1h'] == 0


def test_liquidations_provider_caches_ctval_and_raises_when_all_fail(monkeypatch, tmp_path):
    rows = fx('okx_liq.json')['data'][0]['details']
    now = datetime.fromtimestamp(int(rows[0]['ts']) / 1000 + 60, tz=timezone.utc)
    r = patch(monkeypatch, {**HL, 'public/instruments': fx('okx_instruments.json'),
                            'uly=BTC-USDT': fx('okx_liq.json')})
    p = Liquidations()
    p.state_path = tmp_path / 's.json'
    for _ in range(2):
        res = p.fetch([A['BTC-USD'], A['USDT-USD']], now, now)
    f = by_ticker(res)['BTC-USD']
    assert f['liq_n_1h'] == 12 and set(by_ticker(res)) == {'BTC-USD'}
    assert sum('public/instruments' in u for u in r.calls) == 1  # second tick hit the cache
    assert json.loads(p.state_path.read_text())['okx_ctval']['fetched_at']
    patch(monkeypatch, {})
    bad = Liquidations()
    bad.state_path = tmp_path / 'none.json'
    res = base.run_provider(bad, [A['BTC-USD']], now, now)
    assert not res.ok and 'okx' in res.err


def test_providers_registered():
    from src.ingestion.crypto_flow.providers import all_providers
    assert [p.name for p in all_providers()][-5:] == ['derivs', 'liquidations', 'spot_tape', 'deribit_vol', 'hl_whales']


def test_binance_futures_symbol_rule_and_mark_scaling(monkeypatch):
    f = derivs._binance_futures_symbol
    assert [f(A[t]) for t in ('PEPE-USD', 'BONK-USD', 'SHIB-USD', 'BTC-USD')] == [
        '1000PEPEUSDT', '1000BONKUSDT', '1000SHIBUSDT', 'BTCUSDT']
    assert f(A['USDG-USD']) is None
    hl_none = {'api.hyperliquid.xyz': [{'universe': []}, []]}  # HL lists nothing -> Binance fills mark
    r = patch(monkeypatch, {**hl_none, 'fapi/v1/openInterest': {'symbol': 'x', 'openInterest': '1000'},
                            'premiumIndex': {'markPrice': '0.0123', 'lastFundingRate': '0.0001'}})
    got = by_ticker(Derivs().fetch([A['PEPE-USD']], NOW, NOW))['PEPE-USD']
    assert any('symbol=1000PEPEUSDT' in u for u in r.calls) and not any('symbol=PEPEUSDT' in u for u in r.calls)
    assert got['mark_px'] == pytest.approx(0.0123 / 1000) and got['oi_usd'] == pytest.approx(1000 * 0.0123)


def test_binance_error_dict_skips_asset_without_more_calls(monkeypatch):
    r = patch(monkeypatch, {**HL, 'fapi/v1/openInterest': {'code': -1121, 'msg': 'Invalid symbol.'}})
    sleeps = []
    monkeypatch.setattr(derivs.time, 'sleep', sleeps.append)
    res = Derivs().fetch([A['BTC-USD']], NOW, NOW)
    f = by_ticker(res)
    assert f['BTC-USD']['ls_ratio_global'] is None and f['BTC-USD']['venues'] == 'hyperliquid'
    assert MARKET_TICKER not in f  # Binance answered; not degraded
    assert sum('fapi.binance.com' in u for u in r.calls) == 1
    assert sleeps == [0.1]  # only the inter-asset pause, none between the skipped calls


def test_okx_outage_with_warm_cache_raises(monkeypatch, tmp_path):
    import time
    st = tmp_path / 's.json'
    st.write_text(json.dumps({'okx_ctval': {'fetched_at': time.time(), 'map': {'BTC-USDT-SWAP': 0.01}}}))
    r = patch(monkeypatch, {**HL})
    p = Liquidations()
    p.state_path = st
    res = base.run_provider(p, [A['BTC-USD']], NOW, NOW)
    assert not res.ok and 'okx None for all 1 calls' in res.err
    assert not any('public/instruments' in u for u in r.calls)  # cache really was warm
