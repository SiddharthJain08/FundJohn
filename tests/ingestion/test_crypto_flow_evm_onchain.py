import json
from pathlib import Path

import pytest

from src.ingestion.crypto_flow import registry, universe
from src.ingestion.crypto_flow.providers import evm_onchain
from src.ingestion.crypto_flow.providers.evm_onchain import EvmOnchain, parse_transfers, watched_addresses

FX = Path(__file__).parent / 'fixtures' / 'crypto_flow'
B14 = '0x28c6c06298d514db089934071355e5743bf21d60'
PRICES = {'LINK-USD': 15.0, 'USDT-USD': 1.0, 'PEPE-USD': 4e-6, 'ETH-USD': 3000.0}


def load(name):
    return json.loads((FX / name).read_text())


@pytest.fixture(scope='module')
def abc():
    return evm_onchain.assets_by_contract(universe.load_universe())


def parse(payload, abc, chain_id=1, min_usd=1_000_000):
    return parse_transfers(payload, chain_id=chain_id, exchange_addr=B14, owner='Binance',
                           assets_by_contract=abc, prices=PRICES, min_usd=min_usd)


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv('ETHERSCAN_API_KEY', raising=False)
    assert EvmOnchain().enabled() == (False, 'no_key:ETHERSCAN_API_KEY')


def test_tokentx_maps_contract_to_ticker_and_direction(abc):
    evs, err = parse(load('etherscan_tokentx.json'), abc)
    assert err is None
    by = {e.ticker: e for e in evs}
    assert set(by) == {'LINK-USD', 'USDT-USD'}  # unknown tokens ignored, PEPE below MIN_USD
    link = by['LINK-USD']
    assert link.direction == 'exchange_in' and link.symbol == 'LINK' and link.chain == 'ethereum'
    assert link.source == 'evm_onchain' and link.amount == pytest.approx(100000.0)  # 18 decimals
    assert link.amount_usd == pytest.approx(1_500_000) and link.to_owner == 'Binance' and link.ts_utc.tzinfo
    assert by['USDT-USD'].amount == pytest.approx(2_000_000.0)  # 6 decimals


def test_exchange_out_direction(abc):
    evs, _ = parse(load('etherscan_tokentx.json'), abc, min_usd=1000)
    out = [e for e in evs if e.ticker == 'PEPE-USD' and e.direction == 'exchange_out']
    assert len(out) == 1 and out[0].from_owner == 'Binance'


def test_native_transfer_mapping_and_failed_tx_skipped(abc):
    evs, err = parse(load('etherscan_txlist.json'), abc, min_usd=0.0)
    assert err is None and len(evs) == 1  # zero-value rows and the isError tx dropped
    e = evs[0]
    assert e.ticker == 'ETH-USD' and e.symbol == 'ETH' and e.amount == pytest.approx(1000.0)
    assert e.direction == 'exchange_in' and e.out_index == 0
    arb, _ = parse(load('etherscan_txlist.json'), abc, chain_id=42161)
    assert arb[0].ticker == 'ETH-USD' and arb[0].chain == 'arbitrum'
    bnb, _ = parse(load('etherscan_txlist.json'), abc, chain_id=56)
    assert bnb == []  # BNB is not in the universe


def test_etherscan_rate_limit_message_is_error_not_rows(abc):
    assert parse(load('etherscan_ratelimit.json'), abc) == ([], 'Max rate limit reached')
    assert parse({'status': '0', 'message': 'No transactions found', 'result': []}, abc) == ([], None)


def test_rate_limiter_spaces_calls(monkeypatch):
    slept = []
    monkeypatch.setattr(evm_onchain.time, 'sleep', slept.append)
    t = evm_onchain._Throttle()
    for _ in range(6):
        t.wait()
    assert sum(slept) >= 0.2 and len(slept) == 5


def test_rate_default_is_two_per_second():
    assert evm_onchain._RATE == 2.0
    assert abs(evm_onchain._Throttle().interval - 0.5) < 1e-9


def test_enabled_chains_default_excludes_unsupported_free_tier_chains(monkeypatch):
    monkeypatch.delenv('OPENCLAW_CRYPTO_EVM_CHAINS', raising=False)
    assert evm_onchain.enabled_chain_ids() == [1, 42161, 137]
    monkeypatch.setenv('OPENCLAW_CRYPTO_EVM_CHAINS', 'arb,base,bogus')
    assert evm_onchain.enabled_chain_ids() == [1, 42161, 8453]


def test_watched_set_falls_back_to_eth_registry_on_arb():
    reg = {'ETH': {B14: 'Binance'}, 'ARB': {'0xabc': 'Bitget'}}
    assert watched_addresses(reg, 'ARB') == {B14: 'Binance', '0xabc': 'Bitget'}
    assert watched_addresses(reg, 'BASE') == {B14: 'Binance'}
    assert watched_addresses(registry.load_registry(), 'ARB')[B14] == 'Binance'


def _provider(tmp_path, monkeypatch, calls, payload):
    monkeypatch.setenv('ETHERSCAN_API_KEY', 'k')
    monkeypatch.setattr(evm_onchain.time, 'sleep', lambda s: None)
    p = EvmOnchain()
    p.state_path = tmp_path / 'state.json'
    p.price_hints = dict(PRICES)

    def fake(url, **kw):
        if 'coingecko' in url:
            return {}
        calls.append(url)
        return payload
    monkeypatch.setattr(evm_onchain, 'get_json', fake)
    return p


def test_round_robin_one_chain_per_tick_plus_eth(tmp_path, monkeypatch):
    calls = []
    p = _provider(tmp_path, monkeypatch, calls, {'status': '0', 'message': 'No transactions found', 'result': []})
    assets = universe.load_universe()
    chains = []
    monkeypatch.delenv('OPENCLAW_CRYPTO_EVM_CHAINS', raising=False)  # default rotation = ETH, ARB, POL
    for _ in range(4):
        calls.clear()
        p._fetch(assets, None, None)
        chains.append(sorted({u.split('chainid=')[1].split('&')[0] for u in calls}))
    assert chains[0] == ['1']  # idx 0 is ETH itself
    assert chains[1] == ['1', '42161'] and chains[2] == ['1', '137']
    assert chains[3] == ['1']  # wrapped around after the 3 enabled chains
    assert json.loads(p.state_path.read_text())['evm_next_chain_idx'] == 1


def test_fetch_stores_max_block_and_uses_it_as_startblock(tmp_path, monkeypatch):
    calls = []
    p = _provider(tmp_path, monkeypatch, calls, load('etherscan_txlist.json'))
    assets = universe.load_universe()
    evs, _ = p._fetch(assets, None, None)
    assert len(evs) == 1  # same tx returned for every address, deduped
    assert json.loads(p.state_path.read_text())['evm_last_block']['ETH'] == 26344458
    calls.clear()
    p._fetch(assets, None, None)
    assert 'startblock=26344458' in calls[0]


def test_majority_failed_calls_raise(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch, [], load('etherscan_ratelimit.json'))
    with pytest.raises(evm_onchain.ProviderFetchError, match='calls failed: Max rate limit reached'):
        p._fetch(universe.load_universe(), None, None)


def test_partial_failure_does_not_advance_cursor(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch, [], None)
    good, bad = load('etherscan_txlist.json'), load('etherscan_ratelimit.json')
    n = []

    def fake(url, **kw):
        if 'coingecko' in url:
            return {}
        n.append(1)
        return bad if len(n) == 3 else good  # one failed call, rest fine
    monkeypatch.setattr(evm_onchain, 'get_json', fake)
    evs, _ = p._fetch(universe.load_universe(), None, None)
    assert len(evs) == 1
    assert 'evm_last_block' not in json.loads(p.state_path.read_text()) or \
        'ETH' not in json.loads(p.state_path.read_text())['evm_last_block']


def test_partner_chain_failure_keeps_eth_events_and_state(tmp_path, monkeypatch, capsys):
    p = _provider(tmp_path, monkeypatch, [], None)
    p.state_path.write_text(json.dumps({'evm_next_chain_idx': 1}))  # ARB is the partner chain
    good, bad = load('etherscan_txlist.json'), load('etherscan_ratelimit.json')

    def fake(url, **kw):
        if 'coingecko' in url:
            return {}
        return good if 'chainid=1&' in url else bad
    monkeypatch.setattr(evm_onchain, 'get_json', fake)
    evs, _ = p._fetch(universe.load_universe(), None, None)
    st = json.loads(p.state_path.read_text())
    assert len(evs) == 1 and st['evm_last_block'] == {'ETH': 26344458} and st['evm_next_chain_idx'] == 2
    assert '[evm_onchain] chain=ARB failed' in capsys.readouterr().out
    monkeypatch.setattr(evm_onchain, 'get_json', lambda url, **kw: {} if 'coingecko' in url else bad)
    p.state_path.write_text(json.dumps({'evm_next_chain_idx': 1}))
    with pytest.raises(evm_onchain.ProviderFetchError, match='ETH failed.*ARB failed'):
        p._fetch(universe.load_universe(), None, None)


def test_out_index_discriminates_token_contract_and_native(abc):
    rows = load('etherscan_tokentx.json')['result']
    a = dict(rows[1], transactionIndex='0')
    b = dict(a, contractAddress='0xdac17f958d2ee523a2206206994597c13d831ec7', tokenDecimal='6', value='2000000000000')
    evs, _ = parse({'result': [a, b]}, abc)
    assert len({e.out_index for e in evs}) == 2 and all(e.out_index >= 1 for e in evs)
    nat, _ = parse(load('etherscan_txlist.json'), abc)
    assert nat[0].out_index == 0


def test_error_strings_never_contain_api_key(tmp_path, monkeypatch):
    p = _provider(tmp_path, monkeypatch, [], load('etherscan_ratelimit.json'))
    monkeypatch.setenv('ETHERSCAN_API_KEY', 'SECRETKEY123')
    _, err = parse(load('etherscan_ratelimit.json'), evm_onchain.assets_by_contract(universe.load_universe()))
    assert 'SECRETKEY123' not in err
    for payload in (load('etherscan_ratelimit.json'), None):
        monkeypatch.setattr(evm_onchain, 'get_json', lambda url, _p=payload, **kw: {} if 'coingecko' in url else _p)
        with pytest.raises(evm_onchain.ProviderFetchError) as ei:
            p._fetch(universe.load_universe(), None, None)
        assert 'SECRETKEY123' not in str(ei.value)


def test_deadline_mid_pass_returns_partial_and_does_not_advance_cursor(tmp_path, monkeypatch, capsys):
    import time as _time
    p = _provider(tmp_path, monkeypatch, [], None)
    p.deadline_at = _time.monotonic() + 3600
    n = []

    def fake(url, **kw):
        if 'coingecko' in url:
            return {}
        n.append(1)
        if len(n) == 2:
            p.deadline_at = _time.monotonic() - 1  # tick deadline passes during the 2nd call
        return load('etherscan_txlist.json')
    monkeypatch.setattr(evm_onchain, 'get_json', fake)
    evs, _ = p._fetch(universe.load_universe(), None, None)
    assert len(n) == 2 and isinstance(evs, list) and p.deadline > 0  # partial result returned, no raise
    st = json.loads(p.state_path.read_text())
    assert 'ETH' not in (st.get('evm_last_block') or {})  # un-fetched addresses would lose rows
    assert 'cursor not advanced' in capsys.readouterr().out
