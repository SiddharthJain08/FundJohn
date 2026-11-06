from datetime import datetime, timezone
from src.ingestion.crypto_flow.providers import base
from src.ingestion.crypto_flow import registry, models

def test_get_json_non_json_body_returns_none(monkeypatch):
    monkeypatch.setattr(base, 'fetch_with_retry', lambda *a, **k: b'<!DOCTYPE html><html>error</html>')
    assert base.get_json('https://x/y', label='t') is None

def test_get_json_posts_body(monkeypatch):
    seen = {}
    def fake(req, **k): seen['data'] = req.data; seen['method'] = req.get_method(); return b'{"ok":1}'
    monkeypatch.setattr(base, 'fetch_with_retry', fake)
    assert base.get_json('https://x', label='t', method='POST', body={'type': 'meta'}) == {'ok': 1}
    assert seen['method'] == 'POST' and b'"type"' in seen['data']

class _Boom(base.Provider):
    name = 'boom'; cadence = 'tick'
    def _fetch(self, assets, since, now): raise RuntimeError('kaput')

def test_run_provider_turns_exception_into_failed_result():
    r = base.run_provider(_Boom(), [], datetime.now(timezone.utc), datetime.now(timezone.utc))
    assert r.ok is False and 'kaput' in r.err and r.events == [] and r.latency_ms >= 0

def test_registry_direction_classification():
    reg = registry.load_registry()
    assert reg['ETH'] and reg['BTC']
    owner, typ = registry.owner_of('ETH', '0x28C6c06298d514Db089934071355E5743bf21d60', reg)
    assert owner == 'Binance' and typ == 'exchange'
    assert registry.classify_direction(None, 'exchange') == 'exchange_in'
    assert registry.classify_direction('exchange', None) == 'exchange_out'
    assert registry.classify_direction('exchange', 'exchange') == 'exchange_to_exchange'
    assert registry.classify_direction(None, None) == 'unknown'

class _Off(base.Provider):
    name = 'off'
    def enabled(self): return False, 'no_key:X'

class _Ok(base.Provider):
    name = 'ok'
    def _fetch(self, assets, since, now): return [], [models.Observation(now, 'BTC-USD', 'ok', {'a': 1})]

def test_fetch_skip_and_rows():
    n = datetime.now(timezone.utc)
    r = base.run_provider(_Off(), [], n, n)
    assert r.ok and r.skipped == 'no_key:X' and r.rows == 0
    r = base.run_provider(_Ok(), [], n, n)
    assert r.ok and r.rows == 1 and r.skipped is None

def test_usd_and_owner_edge_cases():
    assert base.usd(2.0, 3.0) == 6.0
    assert base.usd(2.0, None) is None and base.usd(2.0, float('nan')) is None
    reg = {'ETH': {'0xab': 'X'}, 'BTC': {'AbC': 'Y'}}
    assert registry.owner_of('ETH', '0xAB', reg) == ('X', 'exchange')
    assert registry.owner_of('BTC', 'abc', reg) == (None, None)
    assert registry.owner_of('BTC', None, reg) == (None, None)

def test_run_provider_fetch_error_is_failed_result():
    class _P(base.Provider):
        name = 'hl'
        def _fetch(self, assets, since, now): raise base.ProviderFetchError('hyperliquid: None')
    n = datetime.now(timezone.utc)
    r = base.run_provider(_P(), [], n, n)
    assert r.ok is False and r.err == 'ProviderFetchError: hyperliquid: None'

def test_registry_has_no_malformed_addresses():
    import re
    reg = registry.load_registry()
    evm = re.compile(r'^0x[0-9a-f]{40}$')
    b58 = re.compile(r'^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$')
    bech = re.compile(r'^bc1[ac-hj-np-z02-9]{11,71}$')
    sol = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
    xrp = re.compile(r'^r[1-9A-HJ-NP-Za-km-z]{24,34}$')
    for chain, addrs in reg.items():
        for a in addrs:
            if chain == 'BTC':
                assert b58.match(a) or bech.match(a), a
            elif chain in ('ETH', 'ARB', 'POL', 'AVAX', 'BASE', 'BNB'):
                assert evm.match(a), a
            elif chain == 'SOL':
                assert sol.match(a), a
            elif chain == 'XRP':
                assert xrp.match(a), a

def test_registry_rejected_list_is_not_served():
    import json
    raw = json.loads(registry.REGISTRY_PATH.read_text())
    assert raw['_rejected']
    reg = registry.load_registry()
    assert '_rejected' not in reg and '_meta' not in reg
    for r in raw['_rejected']:
        assert reg.get(r['chain'], {}).get(r['address']) is None
