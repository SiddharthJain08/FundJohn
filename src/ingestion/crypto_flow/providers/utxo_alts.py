"""utxo_alts: DOGE / LTC (BlockCypher) and BCH (Blockchair) transfers >= MIN_USD."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from src.ingestion.crypto_flow.models import Observation, WhaleEvent
from src.ingestion.crypto_flow.providers.base import (
    Provider, ProviderFetchError, StateMixin, get_json, min_usd, resolve_prices)

BLOCKCYPHER = 'https://api.blockcypher.com/v1/{coin}/main'
BLOCKCHAIR = 'https://api.blockchair.com/bitcoin-cash/transactions'
COINS = {'doge': ('dogecoin', 'DOGE-USD'), 'ltc': ('litecoin', 'LTC-USD')}
BCH_TICKER = 'BCH-USD'
MAX_BLOCKS_PER_RUN = 2
MAX_TX_PER_BLOCK = 10  # BlockCypher is 3 req/s keyless; txids come in inclusion order
BASE = 1e8
BCH_FLOOR_SAT = 1_000_000_000  # 10 BCH


def _event(*, chain, ticker, ts, amount, price, frm, to, tx_hash, out_index) -> WhaleEvent:
    return WhaleEvent(
        ts_utc=ts, source='utxo_alts', chain=chain, symbol=ticker.removesuffix('-USD'), ticker=ticker,
        amount=amount, amount_usd=amount * price, from_addr=frm, from_owner=None, from_owner_type=None,
        to_addr=to, to_owner=None, to_owner_type=None, tx_type='transfer', tx_hash=tx_hash, out_index=out_index,
        direction='unknown')  # no exchange registry for these chains


def _ts(s: str | None) -> datetime:
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (AttributeError, ValueError):
        return datetime.now(timezone.utc)


def parse_blockcypher_tx(tx, *, coin, price, min_usd) -> list[WhaleEvent]:
    chain, ticker = COINS[coin]
    in_addrs = {a for i in tx.get('inputs', []) for a in i.get('addresses') or []}
    frm = next((a for i in tx.get('inputs', []) for a in i.get('addresses') or []), None)
    events = []
    for idx, o in enumerate(tx.get('outputs', [])):
        addrs = o.get('addresses') or []
        if not addrs or addrs[0] in in_addrs:
            continue  # OP_RETURN / nonstandard, or change back to an input
        amount = (o.get('value') or 0) / BASE
        if amount * price < min_usd:
            continue
        events.append(_event(chain=chain, ticker=ticker, ts=_ts(tx.get('confirmed') or tx.get('received')),
                             amount=amount, price=price, frm=frm, to=addrs[0], tx_hash=tx['hash'], out_index=idx))
    return events


def parse_blockchair(payload, *, price, min_usd) -> list[WhaleEvent]:
    """The list endpoint exposes only totals (no addresses) -> direction 'unknown', out_index 0."""
    events = []
    for r in (payload or {}).get('data') or []:
        amount = (r.get('output_total') or 0) / BASE
        if r.get('is_coinbase') or amount * price < min_usd:
            continue
        ts = datetime.strptime(r['time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        events.append(_event(chain='bitcoin_cash', ticker=BCH_TICKER, ts=ts, amount=amount, price=price,
                             frm=None, to=None, tx_hash=r['hash'], out_index=0))
    return events


class UtxoAlts(StateMixin, Provider):
    name = 'utxo_alts'
    cadence = 'hourly'
    parse_blockcypher_tx = staticmethod(parse_blockcypher_tx)
    parse_blockchair = staticmethod(parse_blockchair)

    def __init__(self):
        self.price_hints: dict[str, float] = {}

    def _bc(self, coin, path='', query=''):
        """BlockCypher GET (token appended when set); paced for the 3 req/s keyless limit.
        URL secrets are redacted by `_http_retry.redact_url` on logging."""
        token = os.environ.get('BLOCKCYPHER_TOKEN')
        time.sleep(0.05 if token else 0.4)
        q = '&'.join(x for x in (query, f'token={token}' if token else '') if x)
        return get_json(BLOCKCYPHER.format(coin=coin) + path + (f'?{q}' if q else ''), label=f'blockcypher_{coin}')

    def _coin(self, coin, price, state_heights, found):
        """-> Observation | None (None = chain tip unreachable)."""
        chain, ticker = COINS[coin]
        tip = self._bc(coin)
        if not isinstance(tip, dict) or not isinstance(tip.get('height'), int):
            return None
        last, height = state_heights.get(coin), tip['height']
        heights = [height] if last is None else list(range(max(last + 1, height - MAX_BLOCKS_PER_RUN + 1), height + 1))
        scanned = total = 0
        for h in heights:
            block = self._bc(coin, f'/blocks/{h}', 'txstart=0&limit=50')
            if not isinstance(block, dict) or not isinstance(block.get('txids'), list):
                break  # retry from here next run
            ids = block['txids'][:MAX_TX_PER_BLOCK]
            for txid in ids:
                tx = self._bc(coin, f'/txs/{txid}')
                if isinstance(tx, dict) and tx.get('hash'):
                    for e in parse_blockcypher_tx(tx, coin=coin, price=price, min_usd=min_usd()):
                        found[e.key()] = e
            scanned += len(ids)
            total += max(int(block.get('n_tx') or 0), len(block['txids']))
            state_heights[coin] = h
        skipped = 0 if last is None else max(0, height - last - len(heights))
        return Observation(datetime.now(timezone.utc), ticker, 'utxo_alts', {
            'utxo_alts_coverage': scanned / total if total else None, 'blocks_scanned': len(heights),
            'blocks_skipped': skipped})

    def _fetch(self, assets, since, now):
        needed = {a.ticker: a for a in assets if a.ticker in {t for _, t in COINS.values()} | {BCH_TICKER}}
        prices = resolve_prices(self.price_hints, needed, label='utxo_alts', fetch=get_json)
        found: dict[tuple, WhaleEvent] = {}
        obs, ok = [], 0
        state = self.load_state()
        heights = dict(state.get('utxo_last_height', {}))
        for coin, (_, ticker) in COINS.items():
            if prices.get(ticker):
                o = self._coin(coin, prices[ticker], heights, found)
                if o is not None:
                    ok, obs = ok + 1, obs + [o]
        if prices.get(BCH_TICKER):
            payload = self._blockchair(prices[BCH_TICKER])
            if payload is not None:
                ok += 1
                for e in parse_blockchair(payload, price=prices[BCH_TICKER], min_usd=min_usd()):
                    found[e.key()] = e
        if not ok:
            raise ProviderFetchError('utxo_alts: BlockCypher and Blockchair both failed (or no prices)')
        if heights != state.get('utxo_last_height', {}):
            self.save_state({**state, 'utxo_last_height': heights})
        return list(found.values()), obs

    def _blockchair(self, price):
        # raise the satoshi floor to the USD threshold so the 50-row page is not wasted on small transfers
        floor = max(BCH_FLOOR_SAT, int(min_usd() / price * BASE))
        key = os.environ.get('BLOCKCHAIR_API_KEY')
        url = f'{BLOCKCHAIR}?q=output_total({floor}..)&s=time(desc)&limit=50' + (f'&key={key}' if key else '')
        data = get_json(url, label='blockchair_bch')
        return data if isinstance(data, dict) and isinstance(data.get('data'), list) else None
