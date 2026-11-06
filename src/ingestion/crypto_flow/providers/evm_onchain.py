"""evm_onchain: exchange-side ERC-20 / native transfers >= MIN_USD via Etherscan V2 on six EVM chains."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from src.ingestion.crypto_flow import registry
from src.ingestion.crypto_flow.models import WhaleEvent
from src.ingestion.crypto_flow.providers.base import Provider, ProviderFetchError, get_json, past_deadline

API_URL = ('https://api.etherscan.io/v2/api?chainid={cid}&module=account&action={action}&address={addr}'
           '&startblock={start}&sort=desc&page=1&offset=100&apikey={key}')
COINGECKO_URL = 'https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd'
CHAINS = {1: 'ETH', 42161: 'ARB', 137: 'POL', 43114: 'AVAX', 8453: 'BASE', 56: 'BNB'}
CHAIN_NAMES = {1: 'ethereum', 42161: 'arbitrum', 137: 'polygon', 43114: 'avalanche', 8453: 'base', 56: 'bnb'}
NATIVE_TICKER = {1: 'ETH-USD', 42161: 'ETH-USD', 8453: 'ETH-USD', 137: 'POL-USD', 43114: 'AVAX-USD'}
_RATE = 5  # requests / second


def _min_usd() -> float:
    return float(os.environ.get('OPENCLAW_CRYPTO_WHALE_MIN_USD', 1_000_000))


def watched_addresses(reg, chain_code: str) -> dict[str, str]:
    """EOA addresses are chain-portable: chain-specific registry entries override the ETH ones."""
    return {**reg.get('ETH', {}), **reg.get(chain_code, {})}


def assets_by_contract(assets) -> dict[tuple, object]:
    """(evm_chainid, contract_lower | None for native) -> Asset."""
    out = {}
    for a in assets:
        if a.evm_chainid and a.token_contract:
            out[(a.evm_chainid, a.token_contract.lower())] = a
    by_t = {a.ticker: a for a in assets}
    for cid, t in NATIVE_TICKER.items():
        if t in by_t:
            out[(cid, None)] = by_t[t]
    return out


def max_block(payload) -> int:
    rows = (payload or {}).get('result')
    return max((int(r['blockNumber']) for r in rows if isinstance(r, dict)), default=0) if isinstance(rows, list) else 0


def parse_transfers(payload, *, chain_id, exchange_addr, owner, assets_by_contract, prices, min_usd,
                    watched=None) -> tuple[list[WhaleEvent], str | None]:
    """Rows of one tokentx/txlist response touching `exchange_addr`. `watched` (addr->owner) detects exchange<->exchange."""
    rows = (payload or {}).get('result')
    if not isinstance(rows, list):
        return [], (rows if isinstance(rows, str) else 'bad payload')
    ex, watched = exchange_addr.lower(), watched or {}
    events = []
    for r in rows:
        if r.get('isError') == '1':
            continue
        is_token = 'tokenDecimal' in r
        asset = assets_by_contract.get((chain_id, (r.get('contractAddress') or '').lower() if is_token else None))
        price = prices.get(asset.ticker) if asset else None
        if asset is None or not price:
            continue
        frm, to = (r.get('from') or '').lower(), (r.get('to') or '').lower()
        if ex not in (frm, to):
            continue
        amount = int(r.get('value') or 0) / 10 ** (int(r['tokenDecimal']) if is_token else 18)
        if amount <= 0 or amount * price < min_usd:
            continue
        f_owner = owner if frm == ex else watched.get(frm)
        t_owner = owner if to == ex else watched.get(to)
        f_type, t_type = ('exchange' if f_owner else None), ('exchange' if t_owner else None)
        events.append(WhaleEvent(
            ts_utc=datetime.fromtimestamp(int(r['timeStamp']), tz=timezone.utc), source='evm_onchain',
            chain=CHAIN_NAMES[chain_id], symbol=asset.ticker.removesuffix('-USD'), ticker=asset.ticker,
            amount=amount, amount_usd=amount * price, from_addr=frm, from_owner=f_owner, from_owner_type=f_type,
            to_addr=to, to_owner=t_owner, to_owner_type=t_type, tx_type='transfer', tx_hash=r['hash'],
            out_index=_out_index(r) if is_token else 0,
            direction=registry.classify_direction(f_type, t_type)))
    return events, None


def _out_index(r) -> int:
    """tokentx has no logIndex: txindex*1000 + 1 + contract discriminator, so it never equals native's 0
    and two token transfers in one tx differ."""
    return int(r.get('transactionIndex') or 0) * 1000 + 1 + int((r.get('contractAddress') or '0')[-4:], 16) % 999


class _Throttle:
    def __init__(self, rate: float = _RATE):
        self.interval, self.last = 1.0 / rate, None

    def wait(self) -> None:
        if self.last is not None:
            gap = self.interval - (time.monotonic() - self.last)
            if gap > 0:
                time.sleep(gap)
        self.last = time.monotonic()


class EvmOnchain(Provider):
    name = 'evm_onchain'
    cadence = 'tick'
    state_path = Path('data/master/.crypto_flow_state.json')
    parse_transfers = staticmethod(parse_transfers)

    def __init__(self):
        self.price_hints: dict[str, float] = {}
        self.throttle = _Throttle()
        self.deadline_at: float | None = None  # time.monotonic() bound, set by the tick driver
        self.deadline = 0  # calls not made because the tick deadline hit

    def enabled(self):
        return (True, None) if os.environ.get('ETHERSCAN_API_KEY') else (False, 'no_key:ETHERSCAN_API_KEY')

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

    def _prices(self, needed: dict) -> dict[str, float]:
        """ticker -> USD: tick-driver hints first, then ONE CoinGecko call for the rest."""
        prices = {t: p for t, p in self.price_hints.items() if t in needed and p}
        missing = {t: a for t, a in needed.items() if t not in prices and a.coingecko_id}
        if missing:
            headers = {'x-cg-demo-api-key': os.environ['COINGECKO_API_KEY']} if os.environ.get('COINGECKO_API_KEY') else None
            ids = ','.join(sorted({a.coingecko_id for a in missing.values()}))
            data = get_json(COINGECKO_URL.format(ids=ids), label='coingecko_simple_price', headers=headers)
            for t, a in missing.items():
                try:
                    prices[t] = float(data[a.coingecko_id]['usd'])
                except (TypeError, KeyError, ValueError):
                    pass
        print(f'[evm_onchain] prices resolved={len(prices)} skipped_no_price={len(needed) - len(prices)}')
        if not prices:
            raise ProviderFetchError('evm_onchain: no prices')
        return prices

    def _pass(self, cid, key, abc, prices, min_usd, reg, found, last):
        """One chain. The cursor only advances when EVERY call succeeded: a failed address would otherwise
        lose its rows below the max block seen from the others (inclusive startblock re-fetches are
        absorbed by the store's dedupe) - simpler than per-address cursors."""
        code, watched = CHAINS[cid], watched_addresses(reg, CHAINS[cid])
        calls = errs = 0
        seen, last_err = last.get(code, 0), None
        for addr, owner in watched.items():
            for action in ('tokentx', 'txlist'):
                if past_deadline(self.deadline_at):
                    self.deadline += 1
                    continue
                self.throttle.wait()
                calls += 1
                # _http_retry truncates logged URLs at 140 chars; the apikey sits past that. Never put the URL in an error.
                payload = get_json(API_URL.format(cid=cid, action=action, addr=addr, start=last.get(code, 0), key=key),
                                   label=f'etherscan_{action}')
                evs, err = parse_transfers(payload, chain_id=cid, exchange_addr=addr, owner=owner,
                                           assets_by_contract=abc, prices=prices, min_usd=min_usd, watched=watched) \
                    if payload else ([], 'no response')
                if err:
                    errs, last_err = errs + 1, err
                    continue
                seen = max(seen, max_block(payload))
                for e in evs:
                    found[(e.chain, e.tx_hash, e.out_index)] = e
        if errs * 2 > calls and calls:
            raise ProviderFetchError(f'evm_onchain: {errs}/{calls} calls failed: {last_err}')
        if errs == 0 and not self.deadline:
            last[code] = seen
        elif self.deadline:  # an incomplete pass must not advance: un-fetched addresses would lose rows
            print(f'[evm_onchain] chain={code} deadline={self.deadline} cursor not advanced')
        else:
            print(f'[evm_onchain] chain={code} {errs}/{calls} calls failed: {last_err}; cursor not advanced')

    def _fetch(self, assets, since, now):
        self.deadline = 0
        key, reg, min_usd = os.environ['ETHERSCAN_API_KEY'], registry.load_registry(), _min_usd()
        abc, state = assets_by_contract(assets), self.load_state()
        order = list(CHAINS)
        idx = state.get('evm_next_chain_idx', 0) % len(order)
        state['evm_next_chain_idx'] = (idx + 1) % len(order)
        self.save_state(state)  # advance first so a failing chain cannot starve the others
        chains = [1] + ([order[idx]] if order[idx] != 1 else [])
        prices = self._prices({a.ticker: a for (cid, _), a in abc.items() if cid in chains})
        found: dict[tuple, WhaleEvent] = {}
        failures = []
        for cid in chains:
            if past_deadline(self.deadline_at):
                print(f'[evm_onchain] deadline=1 chain={CHAINS[cid]} not scanned')
                break
            last = dict(self.load_state().get('evm_last_block', {}))
            try:
                self._pass(cid, key, abc, prices, min_usd, reg, found, last)
            except ProviderFetchError as e:
                failures.append(f'{CHAINS[cid]} failed: {e}')
                print(f'[evm_onchain] chain={CHAINS[cid]} failed: {e}')
                continue
            self.save_state({**self.load_state(), 'evm_last_block': last})  # persist per chain
        if failures and not found:
            raise ProviderFetchError('evm_onchain: ' + '; '.join(failures))
        return list(found.values()), []
