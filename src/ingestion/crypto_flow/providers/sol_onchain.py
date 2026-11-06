"""sol_onchain: SOL + tracked SPL-token (BONK/WIF/TRUMP) transfers >= MIN_USD touching registry exchange wallets."""
from __future__ import annotations

import os
from collections import deque
from datetime import datetime, timezone

from src.ingestion.crypto_flow import registry
from src.ingestion.crypto_flow.models import WhaleEvent
from src.ingestion.crypto_flow.providers.base import (
    Provider, ProviderFetchError, StateMixin, get_json, min_usd, resolve_prices)

PUBLIC_RPC = 'https://api.mainnet-beta.solana.com'
HELIUS_RPC = 'https://mainnet.helius-rpc.com/?api-key={key}'
NATIVE = 'SOL-USD'
BOOTSTRAP_TXS = 5  # first run per address: newest few signatures only
LAMPORTS = 1e9
_TX_OPTS = {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0}


def spl_mints(assets) -> dict:
    """SPL mint -> Asset for tracked Solana tokens."""
    return {a.token_contract: a for a in assets if a.chain == 'solana' and a.token_contract}


def _instructions(tx):
    """(out_index, instruction): top-level by position, inner as (outer+1)*1000+j so every index is unique."""
    for i, ix in enumerate(tx['transaction']['message']['instructions']):
        yield i, ix
    for inner in (tx.get('meta') or {}).get('innerInstructions') or []:
        for j, ix in enumerate(inner.get('instructions', [])):
            yield (inner['index'] + 1) * 1000 + j, ix


def parse_transaction(tx, *, sig, mints, prices, reg, min_usd) -> list[WhaleEvent]:
    """Parsed (jsonParsed) getTransaction result -> SOL / tracked-SPL transfers >= min_usd."""
    if not tx or (tx.get('meta') or {}).get('err') is not None:
        return []
    keys = [k['pubkey'] if isinstance(k, dict) else k for k in tx['transaction']['message']['accountKeys']]
    meta = tx.get('meta') or {}
    owner, mint_of = {}, {}  # token account -> owner wallet / mint (balances reference accounts by index)
    for b in (meta.get('preTokenBalances') or []) + (meta.get('postTokenBalances') or []):
        acct = keys[b['accountIndex']]
        owner[acct], mint_of[acct] = b.get('owner'), b.get('mint')
    ts = datetime.fromtimestamp(tx['blockTime'], tz=timezone.utc)
    events = []
    for idx, ix in _instructions(tx):
        parsed = ix.get('parsed')
        if not isinstance(parsed, dict):
            continue
        info, kind = parsed.get('info') or {}, parsed.get('type')
        if ix.get('program') == 'system' and kind == 'transfer':
            ticker, amount = NATIVE, int(info['lamports']) / LAMPORTS
            frm, to = info.get('source'), info.get('destination')
        elif ix.get('program') == 'spl-token' and kind in ('transfer', 'transferChecked'):
            src, dst = info.get('source'), info.get('destination')
            asset = mints.get(info.get('mint') or mint_of.get(src) or mint_of.get(dst))
            if asset is None:
                continue
            ticker = asset.ticker
            ta = info.get('tokenAmount') or {}
            amount = ta['uiAmount'] if ta.get('uiAmount') is not None else \
                int(ta.get('amount') or info.get('amount') or 0) / 10 ** (ta.get('decimals') or asset.decimals or 0)
            frm, to = owner.get(src) or info.get('authority') or src, owner.get(dst) or dst
        else:
            continue
        price = prices.get(ticker)
        if not price or amount <= 0 or amount * price < min_usd:
            continue
        f_owner, f_type = registry.owner_of('SOL', frm, reg)
        t_owner, t_type = registry.owner_of('SOL', to, reg)
        events.append(WhaleEvent(
            ts_utc=ts, source='sol_onchain', chain='solana', symbol=ticker.removesuffix('-USD'), ticker=ticker,
            amount=amount, amount_usd=amount * price, from_addr=frm, from_owner=f_owner, from_owner_type=f_type,
            to_addr=to, to_owner=t_owner, to_owner_type=t_type, tx_type='transfer', tx_hash=sig, out_index=idx,
            direction=registry.classify_direction(f_type, t_type)))
    return events


class SolOnchain(StateMixin, Provider):
    name = 'sol_onchain'
    cadence = 'hourly'
    parse_transaction = staticmethod(parse_transaction)

    def __init__(self):
        self.price_hints: dict[str, float] = {}
        self.calls = 0

    def rpc_url(self) -> str:
        key = os.environ.get('HELIUS_API_KEY')
        return HELIUS_RPC.format(key=key) if key else PUBLIC_RPC

    def max_calls(self) -> int:
        override = os.environ.get('OPENCLAW_CRYPTO_SOL_MAX_CALLS')
        return int(override) if override else (600 if os.environ.get('HELIUS_API_KEY') else 60)

    def _rpc(self, method, params):
        """result or None. Counts every call against the budget. Never put the URL (api key) in an error."""
        self.calls += 1
        data = get_json(self.rpc_url(), label=f'solana_{method}', method='POST',
                        body={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        return data.get('result') if isinstance(data, dict) else None

    def _queues(self, watched, cursors, budget, failed):
        queues = {}
        for addr in watched:
            if self.calls >= budget:
                break
            cfg = {'limit': 50, **({'until': cursors[addr]} if cursors.get(addr) else {})}
            sigs = self._rpc('getSignaturesForAddress', [addr, cfg])
            if not isinstance(sigs, list):
                failed.append(addr)
                continue
            rows = sigs[:BOOTSTRAP_TXS] if not cursors.get(addr) else sigs  # newest-first
            queues[addr] = deque(reversed(rows))  # process oldest-first so the cursor stays contiguous
        return queues

    def _fetch(self, assets, since, now):
        reg = registry.load_registry()
        watched, mints = reg.get('SOL', {}), spl_mints(assets)
        needed = {a.ticker: a for a in assets if a.ticker == NATIVE or a.token_contract in mints}
        prices = resolve_prices(self.price_hints, needed, label='sol_onchain', fetch=get_json)
        if not watched or not prices:
            if watched:
                raise ProviderFetchError('sol_onchain: no prices')
            return [], []
        state = self.load_state()
        cursors = dict(state.get('sol_last_sig', {}))
        self.calls, budget, failed = 0, self.max_calls(), []
        queues = self._queues(watched, cursors, budget, failed)
        if not queues and failed:
            raise ProviderFetchError(f'sol_onchain: getSignaturesForAddress failed for all {len(failed)} addresses')
        found: dict[tuple, WhaleEvent] = {}
        while queues and self.calls < budget:
            for addr in list(queues):
                q = queues[addr]
                if not q:
                    del queues[addr]
                    continue
                if self.calls >= budget:
                    break
                entry = q[0]
                if entry.get('err') is None:
                    tx = self._rpc('getTransaction', [entry['signature'], _TX_OPTS])
                    if tx is None:  # not yet available / RPC error: retry from here next run
                        del queues[addr]
                        continue
                    for e in parse_transaction(tx, sig=entry['signature'], mints=mints, prices=prices, reg=reg,
                                               min_usd=min_usd()):
                        found[e.key()] = e
                q.popleft()
                cursors[addr] = entry['signature']
        if any(queues.values()) and self.calls >= budget:
            print(f'[sol_onchain] budget_exhausted=True calls={self.calls} pending={sum(map(len, queues.values()))}')
        if cursors != state.get('sol_last_sig', {}):
            self.save_state({**state, 'sol_last_sig': cursors})
        return list(found.values()), []
