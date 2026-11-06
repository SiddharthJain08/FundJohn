"""btc_onchain: BTC transfers >= MIN_USD from blockchain.info mempool + mempool.space blocks."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src.ingestion.crypto_flow import registry
from src.ingestion.crypto_flow.models import WhaleEvent
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json

UNCONFIRMED_URL = 'https://blockchain.info/unconfirmed-transactions?format=json'
BLOCKS_URL = 'https://mempool.space/api/blocks'
RAWBLOCK_URL = 'https://blockchain.info/rawblock/{id}'
BLOCK_TXS_URL = 'https://mempool.space/api/block/{id}/txs'
SPOT_URL = 'https://api.coinbase.com/v2/prices/BTC-USD/spot'
SAT = 1e8
MAX_BLOCKS_PER_TICK = 3


def _min_usd() -> float:
    return float(os.environ.get('OPENCLAW_CRYPTO_WHALE_MIN_USD', 1_000_000))


def _utc(ts) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _emit(txid, ts, ins, outs, *, price, reg, min_usd) -> list[WhaleEvent]:
    """ins: [addr|None]; outs: [(addr|None, sat)] in vout order."""
    in_addrs = {a for a in ins if a}
    from_addr = next((a for a in ins if a), None)
    f_owner, f_type = registry.owner_of('BTC', from_addr, reg)
    events = []
    for idx, (addr, sat) in enumerate(outs):
        if not addr or addr in in_addrs:
            continue  # OP_RETURN / nonstandard, or change back to an input
        amount = sat / SAT
        if amount * price < min_usd:
            continue
        t_owner, t_type = registry.owner_of('BTC', addr, reg)
        events.append(WhaleEvent(
            ts_utc=ts, source='btc_onchain', chain='bitcoin', symbol='BTC', ticker='BTC-USD',
            amount=amount, amount_usd=amount * price, from_addr=from_addr, from_owner=f_owner,
            from_owner_type=f_type, to_addr=addr, to_owner=t_owner, to_owner_type=t_type,
            tx_type='transfer', tx_hash=txid, out_index=idx,
            direction=registry.classify_direction(f_type, t_type)))
    return events


def parse_unconfirmed(payload, *, price, reg, min_usd) -> list[WhaleEvent]:
    events = []
    for tx in (payload or {}).get('txs', []):
        ins = [(i.get('prev_out') or {}).get('addr') for i in tx.get('inputs', [])]
        outs = [(o.get('addr'), o.get('value') or 0) for o in tx.get('out', [])]
        events += _emit(tx['hash'], _utc(tx['time']), ins, outs, price=price, reg=reg, min_usd=min_usd)
    return events


def parse_block_txs(txs, *, price, reg, min_usd, ts=None) -> list[WhaleEvent]:
    ts = ts or datetime.now(timezone.utc)
    events = []
    for tx in txs or []:
        ins = [(i.get('prevout') or {}).get('scriptpubkey_address') for i in tx.get('vin', [])]
        outs = [(o.get('scriptpubkey_address'), o.get('value') or 0) for o in tx.get('vout', [])]
        events += _emit(tx['txid'], ts, ins, outs, price=price, reg=reg, min_usd=min_usd)
    return events


class BtcOnchain(Provider):
    name = 'btc_onchain'
    cadence = 'tick'
    price_hint: float | None = None
    state_path = Path('data/master/.crypto_flow_state.json')
    parse_unconfirmed = staticmethod(parse_unconfirmed)
    parse_block_txs = staticmethod(parse_block_txs)

    def load_state(self) -> dict:
        try:
            return json.loads(Path(self.state_path).read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)

    def _price(self) -> float:
        price = self.price_hint
        if price is None:
            spot = get_json(SPOT_URL, label='coinbase_spot')
            try:
                price = float(spot['data']['amount'])
            except (TypeError, KeyError, ValueError):
                price = None
        if not price or price <= 0:
            raise ProviderFetchError('btc_onchain: no price')
        return price

    def _fetch(self, assets, since, now):
        price, reg, min_usd = self._price(), registry.load_registry(), _min_usd()
        unconf = get_json(UNCONFIRMED_URL, label='blockchain_info_unconf')
        blocks = get_json(BLOCKS_URL, label='mempool_blocks')
        if unconf is None and not isinstance(blocks, list):
            raise ProviderFetchError('btc_onchain: blockchain.info and mempool.space both failed')
        found: dict[tuple, WhaleEvent] = {}
        for e in parse_unconfirmed(unconf, price=price, reg=reg, min_usd=min_usd):
            found[(e.tx_hash, e.out_index)] = e
        if isinstance(blocks, list):
            state = self.load_state()
            last = state.get('btc_last_height')
            new = sorted((b for b in blocks if last is None or b['height'] > last),
                         key=lambda b: b['height'], reverse=True)
            new = new[:1] if last is None else new[:MAX_BLOCKS_PER_TICK]
            done = last
            for b in sorted(new, key=lambda b: b['height']):
                ts = _utc(b['timestamp'])
                raw = get_json(RAWBLOCK_URL.format(id=b['id']), label='blockchain_info_rawblock', timeout=20)
                if isinstance(raw, dict) and isinstance(raw.get('tx'), list):
                    evs = parse_unconfirmed({'txs': raw['tx']}, price=price, reg=reg, min_usd=min_usd)
                else:
                    print(f"[btc_onchain] rawblock miss height={b['height']} fallback=mempool_page")
                    txs = get_json(BLOCK_TXS_URL.format(id=b['id']), label='mempool_block_txs')
                    if not isinstance(txs, list):
                        break  # retry this block next tick; keep heights contiguous
                    evs = parse_block_txs(txs, price=price, reg=reg, min_usd=min_usd, ts=ts)
                for e in evs:
                    found[(e.tx_hash, e.out_index)] = e
                done = b['height']
            if done != last:
                self.save_state({**state, 'btc_last_height': done})
        return list(found.values()), []
