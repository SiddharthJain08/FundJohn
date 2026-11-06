import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingestion.crypto_flow import registry
from src.ingestion.crypto_flow.providers import btc_onchain, base

FX = Path(__file__).parent / 'fixtures' / 'crypto_flow'
BINANCE = '34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo'


@pytest.fixture
def fixture_json():
    return json.loads((FX / 'blockchain_info_unconfirmed.json').read_text())


@pytest.fixture
def block_fx():
    return json.loads((FX / 'mempool_block_txs.json').read_text())


def test_parses_unconfirmed_and_emits_only_large_outputs(fixture_json):
    p = btc_onchain.BtcOnchain()
    evs = p.parse_unconfirmed(fixture_json, price=76000.0, reg=registry.load_registry(), min_usd=1_000_000)
    assert all(e.amount_usd >= 1_000_000 for e in evs) and len(evs) == 1
    e = evs[0]
    assert e.chain == 'bitcoin' and e.ticker == 'BTC-USD' and e.symbol == 'BTC' and e.out_index == 0
    assert e.source == 'btc_onchain' and e.tx_type == 'transfer' and e.amount == pytest.approx(14.0)
    assert e.ts_utc.tzinfo is not None


def test_change_output_back_to_input_is_skipped(fixture_json):
    tx = fixture_json['txs'][1]
    assert tx['out'][0]['addr'] == tx['inputs'][0]['prev_out']['addr']
    evs = btc_onchain.parse_unconfirmed(fixture_json, price=76000.0, reg=registry.load_registry(), min_usd=1_000_000)
    assert tx['hash'] not in {e.tx_hash for e in evs}


def test_registry_direction_exchange_in(fixture_json):
    evs = btc_onchain.parse_unconfirmed(fixture_json, price=76000.0, reg=registry.load_registry(), min_usd=1)
    e = next(e for e in evs if e.to_addr == BINANCE)
    assert e.direction == 'exchange_in' and e.to_owner == 'Binance' and e.to_owner_type == 'exchange'
    assert e.from_owner is None


def test_parse_block_txs(block_fx):
    evs = btc_onchain.parse_block_txs(block_fx['txs'], block_fx['height'], price=76000.0,
                                      reg=registry.load_registry(), min_usd=1_000_000,
                                      ts=datetime.fromtimestamp(block_fx['timestamp'], tz=timezone.utc))
    assert len(evs) == 1 and evs[0].out_index == 0 and evs[0].tx_hash == block_fx['txs'][0]['txid']


def _provider(tmp_path, monkeypatch, routes):
    p = btc_onchain.BtcOnchain()
    p.state_path = tmp_path / 'state.json'
    p.price_hint = 76000.0
    monkeypatch.setattr(btc_onchain, 'get_json', lambda url, **k: routes(url))
    return p


def test_fetch_survives_mempool_failure(monkeypatch, tmp_path, fixture_json):
    p = _provider(tmp_path, monkeypatch, lambda u: fixture_json if 'blockchain.info' in u else None)
    now = datetime.now(timezone.utc)
    evs, obs = p._fetch([], now, now)
    assert len(evs) == 1 and obs == []


def test_fetch_raises_when_both_sources_fail(monkeypatch, tmp_path):
    p = _provider(tmp_path, monkeypatch, lambda u: None)
    now = datetime.now(timezone.utc)
    with pytest.raises(base.ProviderFetchError):
        p._fetch([], now, now)


def test_fetch_blocks_state_and_dedupe(monkeypatch, tmp_path, fixture_json, block_fx):
    h = block_fx['height']
    blocks = [{'id': 'b2', 'height': h, 'timestamp': block_fx['timestamp']},
              {'id': 'b1', 'height': h - 1, 'timestamp': block_fx['timestamp']}]

    def routes(u):
        if 'blockchain.info' in u:
            return fixture_json
        if u.endswith('/api/blocks'):
            return blocks
        if '/api/block/b2/txs' in u:
            return block_fx['txs']
        return None
    p = _provider(tmp_path, monkeypatch, routes)
    now = datetime.now(timezone.utc)
    evs, _ = p._fetch([], now, now)  # first run: newest block only
    assert len(evs) == 2 and p.load_state()['btc_last_height'] == h
    evs, _ = p._fetch([], now, now)  # nothing new; mempool event again (store dedupes)
    assert len(evs) == 1


def test_min_usd_env_read_at_call_time(monkeypatch, tmp_path, fixture_json):
    monkeypatch.setenv('OPENCLAW_CRYPTO_WHALE_MIN_USD', '1')
    p = _provider(tmp_path, monkeypatch, lambda u: fixture_json if 'blockchain.info' in u else None)
    now = datetime.now(timezone.utc)
    assert len(p._fetch([], now, now)[0]) > 1


def test_no_price_raises(monkeypatch, tmp_path, fixture_json):
    p = _provider(tmp_path, monkeypatch, lambda u: fixture_json)
    p.price_hint = None
    now = datetime.now(timezone.utc)
    with pytest.raises(base.ProviderFetchError):
        p._fetch([], now, now)
