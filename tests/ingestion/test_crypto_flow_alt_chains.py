import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingestion.crypto_flow import registry, universe
from src.ingestion.crypto_flow.providers import sol_onchain, utxo_alts, xrp_onchain
from src.ingestion.crypto_flow.providers.utxo_alts import UtxoAlts
from src.ingestion.crypto_flow.providers.base import ProviderFetchError
from src.ingestion.crypto_flow.providers.sol_onchain import SolOnchain

from src.ingestion.crypto_flow.providers.xrp_onchain import XrpOnchain

FX = Path(__file__).parent / 'fixtures' / 'crypto_flow'
NOW = datetime(2026, 11, 6, 22, 0, tzinfo=timezone.utc)
BIN = 'rs8ZPbYqgecRcDzQpJYAMhSxSi5htsjnza'


def load(name):
    return json.loads((FX / name).read_text())


@pytest.fixture(scope='module')
def reg():
    return registry.load_registry()


@pytest.fixture(scope='module')
def assets():
    return universe.load_universe()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(utxo_alts.time, 'sleep', lambda s: None)
    monkeypatch.delenv('OPENCLAW_CRYPTO_WHALE_MIN_USD', raising=False)


# ---------------------------------------------------------------- XRP

def test_xrp_parse_account_tx_direction_and_amount(reg):
    evs, max_ledger = xrp_onchain.parse_account_tx(load('xrpl_account_tx.json'), price=2.0, reg=reg, min_usd=1_000_000)
    assert max_ledger == 108115906
    by = sorted(evs, key=lambda e: -e.amount)
    assert [round(e.amount) for e in by] == [368477930, 30_000_000, 11141436]  # failed copy + dust + IOU + TrustSet dropped
    big = by[0]
    assert big.direction == 'exchange_out' and big.from_owner == 'Binance' and big.ticker == 'XRP-USD'
    assert big.chain == 'ripple' and big.source == 'xrp_onchain' and big.amount_usd == pytest.approx(big.amount * 2.0)
    assert big.ts_utc == datetime.fromtimestamp(847134520 + 946684800, tz=timezone.utc) and big.ts_utc.year == 2026
    assert by[2].direction == 'exchange_in' and by[2].to_owner == 'Binance'


def test_xrp_iou_amount_skipped(reg):
    iou = {'result': {'transactions': [{'tx': {
        'TransactionType': 'Payment', 'Account': BIN, 'Destination': 'rPz2qAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        'Amount': {'currency': 'USD', 'issuer': 'rX', 'value': '99999999'}, 'hash': 'A', 'date': 1, 'ledger_index': 5},
        'meta': {'TransactionResult': 'tesSUCCESS'}}]}}
    assert xrp_onchain.parse_account_tx(iou, price=1.0, reg=reg, min_usd=1)[0] == []


def _xrp(tmp_path, monkeypatch, responder):
    p = XrpOnchain()
    p.state_path = tmp_path / 's.json'
    p.price_hints = {'XRP-USD': 2.0}
    calls = []

    def fake(url, **kw):
        calls.append(kw.get('body'))
        return responder(kw.get('body'))
    monkeypatch.setattr(xrp_onchain, 'get_json', fake)
    return p, calls


def test_xrp_provider_cursor_and_request_shape(tmp_path, monkeypatch, assets):
    p, calls = _xrp(tmp_path, monkeypatch, lambda b: load('xrpl_account_tx.json'))
    r = p.fetch(assets, NOW, NOW)
    assert r.ok and r.events
    params = calls[0]['params'][0]
    assert calls[0]['method'] == 'account_tx' and params['limit'] == 50 and params['ledger_index_min'] == -1
    assert p.load_state()['xrp_last_ledger'] == 108115906
    p.fetch(assets, NOW, NOW)
    assert calls[-1]['params'][0]['ledger_index_min'] == 108115906


def test_xrp_all_endpoints_none_raises(tmp_path, monkeypatch, assets):
    p, _ = _xrp(tmp_path, monkeypatch, lambda b: None)
    with pytest.raises(ProviderFetchError):
        p.fetch(assets, NOW, NOW)
    assert 'xrp_last_ledger' not in p.load_state()


# ---------------------------------------------------------------- SOL

PRICES = {'SOL-USD': 150.0, 'TRUMP-USD': 5.0, 'BONK-USD': 2e-5, 'WIF-USD': 1.0}


def test_sol_parse_transaction_sol_and_spl(reg, assets):
    evs = sol_onchain.parse_transaction(load('solana_tx.json')['result'], sig='S', mints=sol_onchain.spl_mints(assets),
                                        prices=PRICES, reg=reg, min_usd=100_000)
    by = {e.ticker: e for e in evs}
    assert set(by) == {'SOL-USD', 'TRUMP-USD'}
    sol, trump = by['SOL-USD'], by['TRUMP-USD']
    assert sol.amount == pytest.approx(2500.0) and sol.direction == 'exchange_to_exchange' and sol.chain == 'solana'
    assert trump.amount == pytest.approx(6943531.803) and trump.symbol == 'TRUMP'
    assert trump.out_index != sol.out_index
    assert trump.ts_utc == datetime.fromtimestamp(load('solana_tx.json')['result']['blockTime'], tz=timezone.utc)


def test_sol_token_account_owner_mapping(reg, assets):
    evs = sol_onchain.parse_transaction(load('solana_tx.json')['result'], sig='S', mints=sol_onchain.spl_mints(assets),
                                        prices=PRICES, reg=reg, min_usd=100_000)
    trump = next(e for e in evs if e.ticker == 'TRUMP-USD')
    # instruction addresses are TOKEN accounts; events must carry the owner WALLETS
    assert trump.from_addr == '5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9'
    assert trump.to_addr == '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM'
    assert trump.from_owner == trump.to_owner == 'Binance'


def test_sol_unknown_mint_and_failed_tx_ignored(reg, assets):
    tx = load('solana_tx.json')['result']
    mints = {k: v for k, v in sol_onchain.spl_mints(assets).items() if k != '6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN'}
    evs = sol_onchain.parse_transaction(tx, sig='S', mints=mints, prices=PRICES, reg=reg, min_usd=1)
    assert [e.ticker for e in evs] == ['SOL-USD']
    tx['meta']['err'] = {'InstructionError': [0, 'x']}
    assert sol_onchain.parse_transaction(tx, sig='S', mints=mints, prices=PRICES, reg=reg, min_usd=1) == []


def _sol(tmp_path, monkeypatch, responder, helius=None):
    if helius:
        monkeypatch.setenv('HELIUS_API_KEY', helius)
    else:
        monkeypatch.delenv('HELIUS_API_KEY', raising=False)
    monkeypatch.delenv('OPENCLAW_CRYPTO_SOL_MAX_CALLS', raising=False)
    p = SolOnchain()
    p.state_path = tmp_path / 's.json'
    p.price_hints = dict(PRICES)
    calls = []

    def fake(url, **kw):
        calls.append((url, kw.get('body')))
        return responder(url, kw.get('body'))
    monkeypatch.setattr(sol_onchain, 'get_json', fake)
    return p, calls


def _many_sigs(addr):
    return {'result': [{'signature': f'{addr[:4]}{i:03d}', 'err': None, 'blockTime': 1} for i in range(50, 0, -1)]}


def _rpc(url, body):
    if body['method'] == 'getSignaturesForAddress':
        return _many_sigs(body['params'][0])
    return load('solana_tx.json')


def test_sol_public_budget_stops_at_60_calls(tmp_path, monkeypatch, assets, capsys):
    p, calls = _sol(tmp_path, monkeypatch, _rpc)
    p.state_path.write_text(json.dumps({'sol_last_sig': {a: 'old' for a in registry.load_registry()['SOL']}}))
    r = p.fetch(assets, NOW, NOW)
    assert r.ok and len(calls) == 60
    assert all(u.startswith('https://api.mainnet-beta.solana.com') for u, _ in calls)
    assert 'budget_exhausted=True' in capsys.readouterr().out
    assert r.events  # parsed what it could fetch


def test_sol_helius_budget_and_url(tmp_path, monkeypatch):
    p, calls = _sol(tmp_path, monkeypatch, _rpc, helius='KEY123')
    assert p.max_calls() == 600 and p.rpc_url() == 'https://mainnet.helius-rpc.com/?api-key=KEY123'
    monkeypatch.setenv('OPENCLAW_CRYPTO_SOL_MAX_CALLS', '7')
    assert p.max_calls() == 7


def test_sol_cursor_advances_to_last_processed_and_bootstraps_small(tmp_path, monkeypatch, assets):
    p, calls = _sol(tmp_path, monkeypatch, _rpc)
    p.fetch(assets, NOW, NOW)  # no cursor: bootstrap newest few only, not 50 per address
    gets = [b for _, b in calls if b['method'] == 'getTransaction']
    assert 0 < len(gets) <= 7 * sol_onchain.BOOTSTRAP_TXS
    cur = p.load_state()['sol_last_sig']
    assert set(cur) == set(registry.load_registry()['SOL']) and all(v.endswith('050') for v in cur.values())


def test_sol_until_param_uses_cursor(tmp_path, monkeypatch, assets):
    p, calls = _sol(tmp_path, monkeypatch, lambda u, b: {'result': []})
    p.state_path.write_text(json.dumps({'sol_last_sig': {'5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9': 'abc'}}))
    p.fetch(assets, NOW, NOW)
    by_addr = {b['params'][0]: b['params'][1] for _, b in calls}
    assert by_addr['5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9'] == {'limit': 50, 'until': 'abc'}
    assert by_addr['9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM'] == {'limit': 50}


def test_sol_all_rpc_none_raises(tmp_path, monkeypatch, assets):
    p, _ = _sol(tmp_path, monkeypatch, lambda u, b: None)
    with pytest.raises(ProviderFetchError):
        p.fetch(assets, NOW, NOW)


# ---------------------------------------------------------------- UTXO

def test_blockcypher_tx_parse_skips_change(reg):
    evs = utxo_alts.parse_blockcypher_tx(load('blockcypher_tx.json'), coin='doge', price=0.2, min_usd=1000)
    assert len(evs) == 1  # output 1 pays back to the input address
    e = evs[0]
    assert e.amount == pytest.approx(192956.17022511) and e.chain == 'dogecoin' and e.ticker == 'DOGE-USD'
    assert e.to_addr == 'DCqNmZtSZv8Luef7i9jfoLxE3r93ZBLuMh' and e.from_addr == 'DM9Vr9A28CY6tQicB3oudfrLJ2jusJoieH'
    assert e.direction == 'unknown' and e.out_index == 0 and e.source == 'utxo_alts'
    assert utxo_alts.parse_blockcypher_tx(load('blockcypher_tx.json'), coin='doge', price=0.2, min_usd=1e9) == []


def test_blockchair_parse_unknown_direction():
    evs = utxo_alts.parse_blockchair(load('blockchair_txs.json'), price=500.0, min_usd=10_000)
    assert evs and all(e.direction == 'unknown' and e.from_addr is None and e.to_addr is None for e in evs)
    assert all(e.ticker == 'BCH-USD' and e.chain == 'bitcoin_cash' and e.amount_usd >= 10_000 for e in evs)
    assert evs[0].amount == pytest.approx(load('blockchair_txs.json')['data'][0]['output_total'] / 1e8)


def _utxo(tmp_path, monkeypatch, responder, token=None):
    if token:
        monkeypatch.setenv('BLOCKCYPHER_TOKEN', token)
    else:
        monkeypatch.delenv('BLOCKCYPHER_TOKEN', raising=False)
    monkeypatch.delenv('BLOCKCHAIR_API_KEY', raising=False)
    p = UtxoAlts()
    p.state_path = tmp_path / 's.json'
    p.price_hints = {'DOGE-USD': 0.2, 'LTC-USD': 100.0, 'BCH-USD': 500.0}
    urls = []

    def fake(url, **kw):
        urls.append(url)
        return responder(url)
    monkeypatch.setattr(utxo_alts, 'get_json', fake)
    return p, urls


def _bc(url):
    if '/blocks/' in url:
        return load('blockcypher_block.json')
    if '/txs/' in url:
        return load('blockcypher_tx.json')
    if 'blockchair' in url:
        return load('blockchair_txs.json')
    return {'height': 6446860}


def test_utxo_alts_token_appended_when_env_set(tmp_path, monkeypatch, assets):
    p, urls = _utxo(tmp_path, monkeypatch, _bc, token='TOK')
    p.fetch(assets, NOW, NOW)
    bc = [u for u in urls if 'blockcypher' in u]
    assert bc and all('token=TOK' in u for u in bc)
    assert 'https://api.blockcypher.com/v1/doge/main?token=TOK' in bc
    assert any('/blocks/6446860?txstart=0&limit=50&token=TOK' in u for u in bc)
    p2, urls2 = _utxo(tmp_path, monkeypatch, _bc)
    p2.fetch(assets, NOW, NOW)
    assert not any('token=' in u for u in urls2 if 'blockcypher' in u)


def test_utxo_alts_coverage_frac_reported(tmp_path, monkeypatch, assets):
    block = load('blockcypher_block.json')
    block['n_tx'], block['txids'] = 100, [f'{i:064x}' for i in range(50)]
    p, urls = _utxo(tmp_path, monkeypatch, lambda u: block if '/blocks/' in u else _bc(u))
    r = p.fetch(assets, NOW, NOW)
    assert sum('/txs/' in u for u in urls if 'doge' in u) == utxo_alts.MAX_TX_PER_BLOCK == 10
    obs = {o.ticker: o for o in r.observations}
    assert obs['DOGE-USD'].fields['utxo_alts_coverage'] == pytest.approx(0.1)
    assert obs['DOGE-USD'].source == 'utxo_alts'


def test_utxo_alts_cursor_limits_to_two_blocks(tmp_path, monkeypatch, assets):
    p, urls = _utxo(tmp_path, monkeypatch, _bc)
    p.state_path.write_text(json.dumps({'utxo_last_height': {'doge': 6446850, 'ltc': 6446860}}))
    p.fetch(assets, NOW, NOW)
    heights = [u.split('/blocks/')[1].split('?')[0] for u in urls if 'doge/main/blocks' in u]
    assert heights == ['6446859', '6446860']
    assert p.load_state()['utxo_last_height']['doge'] == 6446860


def test_utxo_alts_all_sources_none_raises(tmp_path, monkeypatch, assets):
    p, _ = _utxo(tmp_path, monkeypatch, lambda u: None)
    with pytest.raises(ProviderFetchError):
        p.fetch(assets, NOW, NOW)
