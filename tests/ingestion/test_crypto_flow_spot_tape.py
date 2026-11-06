import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingestion.crypto_flow import universe
from src.ingestion.crypto_flow.providers import spot_tape
from src.ingestion.crypto_flow.providers.base import ProviderFetchError
from src.ingestion.crypto_flow.providers.spot_tape import (
    SpotTape, binance_trades, classify_prints, coinbase_trades, kraken_trades)
from src.ingestion.crypto_flow.universe import Asset

FX = Path(__file__).parent / 'fixtures' / 'crypto_flow'
NOW = datetime(2026, 11, 6, 21, 0, tzinfo=timezone.utc)
A = {a.ticker: a for a in universe.load_universe()}
BTC = A['BTC-USD']


def fx(name):
    return json.loads((FX / name).read_text())['payload']


class Router:
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
    monkeypatch.setattr(spot_tape.time, 'sleep', lambda s: None)
    monkeypatch.setattr(spot_tape, 'fetch_hl', lambda: None)  # ranking: BTC/ETH only


def patch(monkeypatch, routes):
    r = Router(routes)
    monkeypatch.setattr(spot_tape, 'get_json', r)
    return r


def test_binance_maker_flag_inverts_to_taker_side():
    t = binance_trades(fx('binance_aggtrades.json'))
    assert len(t) == 20
    assert t[3][2] == 'sell' and t[7][2] == 'buy'  # m=true => taker sold; m=false => taker bought
    assert t[3][1] == 4.0 and t[7][1] == 5.0


def test_coinbase_side_is_maker_side_inverted():
    t = coinbase_trades(fx('coinbase_trades.json'))
    assert t[3][2] == 'sell'  # maker side 'buy' => taker sold
    assert t[7][2] == 'buy'   # maker side 'sell' => taker bought


def test_kraken_taker_side_and_pair_name():
    p = fx('kraken_trades.json')
    t = kraken_trades(p, 'XBTUSD')  # result key is XXBTZUSD
    assert len(t) == 20 and t[3][2] == 'sell' and t[7][2] == 'buy'
    assert spot_tape.kraken_pair(BTC) == 'XBTUSD'
    assert spot_tape.kraken_pair(A['ETH-USD']) == 'ETHUSD'
    assert kraken_trades({'error': ['EQuery:Unknown asset pair'], 'result': {}}, 'XBTUSD') is None


def test_prints_below_threshold_not_counted():
    trades = [(100.0, 2499.0, 'buy'), (100.0, 2500.0, 'buy'), (100.0, 3000.0, 'sell')]
    r = classify_prints(trades, 250_000)
    assert r['large_prints_n'] == 2
    assert r['large_prints_usd_buy'] == 250_000 and r['large_prints_usd_sell'] == 300_000
    assert r['large_print_imbalance_1h'] == pytest.approx(-50_000 / 550_000)
    none = classify_prints([(1.0, 1.0, 'buy')], 250_000)
    assert none['large_prints_n'] == 0 and none['large_print_imbalance_1h'] is None


def test_falls_back_to_coinbase_when_binance_none(monkeypatch):
    r = patch(monkeypatch, {'api.exchange.coinbase.com/products/BTC-USD': fx('coinbase_trades.json')})
    res = SpotTape().fetch([BTC], NOW, NOW)
    f = res.observations[0].fields
    assert f['tape_venue'] == 'coinbase' and f['large_prints_n'] == 2
    assert f['large_prints_usd_buy'] > 250_000 and f['large_prints_usd_sell'] > 250_000
    assert f['tape_n'] == 20 and f['tape_coverage_s'] > 0
    assert sum('binance' in c for c in r.calls) == 1


def test_binance_primary_and_degraded_skip(monkeypatch):
    r = patch(monkeypatch, {'api.binance.com': fx('binance_aggtrades.json'),
                            'products/ETH-USD': fx('coinbase_trades.json')})
    res = SpotTape().fetch([BTC, A['ETH-USD'], A['USDT-USD']], NOW, NOW)
    by = {o.ticker: o.fields['tape_venue'] for o in res.observations}
    assert by == {'BTC-USD': 'binance', 'ETH-USD': 'binance'}  # stable skipped
    r2 = patch(monkeypatch, {'products/': fx('coinbase_trades.json')})
    SpotTape().fetch([BTC, A['ETH-USD']], NOW, NOW)
    assert sum('binance' in c for c in r2.calls) == 1  # first Binance miss => skipped for the tick


def test_kraken_last_fallback(monkeypatch):
    a = Asset(ticker='BTC-USD', alpaca='BTC/USD', position='BTCUSD')  # no binance/coinbase symbol
    patch(monkeypatch, {'api.kraken.com': fx('kraken_trades.json')})
    res = SpotTape().fetch([a], NOW, NOW)
    assert res.observations[0].fields['tape_venue'] == 'kraken'


def test_no_venue_answers_raises(monkeypatch):
    patch(monkeypatch, {})
    with pytest.raises(ProviderFetchError):
        SpotTape().fetch([BTC], NOW, NOW)


def test_deadline_stops_tape_loop_without_raising(monkeypatch):
    import time as _time
    p = SpotTape()
    p.deadline_at = _time.monotonic() - 1  # already past: nothing fetched, not a failure
    r = patch(monkeypatch, {'api.binance.com': fx('binance_aggtrades.json')})
    res = p.fetch(list(A.values()), NOW, NOW)
    assert res.ok and res.observations == [] and r.calls == [] and p.deadline >= 2
