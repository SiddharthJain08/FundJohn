"""xrp_onchain: XRP Payments >= MIN_USD touching registry exchange addresses (XRPL public cluster, account_tx)."""
from __future__ import annotations

from datetime import datetime, timezone

from src.ingestion.crypto_flow import registry
from src.ingestion.crypto_flow.models import WhaleEvent
from src.ingestion.crypto_flow.providers.base import (
    Provider, ProviderFetchError, StateMixin, get_json, min_usd, resolve_prices)

URL = 'https://xrplcluster.com/'
TICKER = 'XRP-USD'
RIPPLE_EPOCH = 946684800  # 2000-01-01 in Unix seconds
DROPS = 1e6


def parse_account_tx(payload, *, price, reg, min_usd) -> tuple[list[WhaleEvent], int]:
    """(events, max ledger_index seen). Only successful XRP Payments; IOU amounts (dict) are skipped."""
    rows = ((payload or {}).get('result') or {}).get('transactions')
    if not isinstance(rows, list):
        return [], 0
    events, max_ledger = [], 0
    for r in rows:
        tx = r.get('tx') or {}
        max_ledger = max(max_ledger, int(tx.get('ledger_index') or 0))
        amt = tx.get('Amount')
        if (tx.get('TransactionType') != 'Payment' or (r.get('meta') or {}).get('TransactionResult') != 'tesSUCCESS'
                or not isinstance(amt, str)):
            continue
        amount = int(amt) / DROPS
        if amount * price < min_usd:
            continue
        frm, to = tx.get('Account'), tx.get('Destination')
        f_owner, f_type = registry.owner_of('XRP', frm, reg)
        t_owner, t_type = registry.owner_of('XRP', to, reg)
        events.append(WhaleEvent(
            ts_utc=datetime.fromtimestamp(int(tx['date']) + RIPPLE_EPOCH, tz=timezone.utc), source='xrp_onchain',
            chain='ripple', symbol='XRP', ticker=TICKER, amount=amount, amount_usd=amount * price,
            from_addr=frm, from_owner=f_owner, from_owner_type=f_type, to_addr=to, to_owner=t_owner,
            to_owner_type=t_type, tx_type='transfer', tx_hash=tx['hash'], out_index=0,
            direction=registry.classify_direction(f_type, t_type)))
    return events, max_ledger


class XrpOnchain(StateMixin, Provider):
    name = 'xrp_onchain'
    cadence = 'tick'
    parse_account_tx = staticmethod(parse_account_tx)

    def __init__(self):
        self.price_hints: dict[str, float] = {}

    def _fetch(self, assets, since, now):
        reg = registry.load_registry()
        watched = reg.get('XRP', {})
        asset = next((a for a in assets if a.ticker == TICKER), None)
        if not watched or asset is None:
            return [], []
        price = resolve_prices(self.price_hints, {TICKER: asset}, label='xrp_onchain', fetch=get_json).get(TICKER)
        if not price:
            raise ProviderFetchError('xrp_onchain: no price')
        state = self.load_state()
        last = state.get('xrp_last_ledger')
        found: dict[tuple, WhaleEvent] = {}
        seen, failed = last or 0, 0
        for addr in watched:
            payload = get_json(URL, label='xrpl_account_tx', method='POST', body={
                'method': 'account_tx', 'params': [{'account': addr, 'limit': 50,
                                                    'ledger_index_min': last if last is not None else -1,
                                                    'ledger_index_max': -1}]})
            if not isinstance(payload, dict) or (payload.get('result') or {}).get('status') != 'success':
                failed += 1
                continue
            evs, max_ledger = parse_account_tx(payload, price=price, reg=reg, min_usd=min_usd())
            seen = max(seen, max_ledger)
            for e in evs:
                found[(e.tx_hash, e.out_index)] = e
        if failed == len(watched):
            raise ProviderFetchError(f'xrp_onchain: all {failed} account_tx calls failed')
        if failed == 0 and seen != last:  # a failed address would lose rows below the others' max ledger
            self.save_state({**state, 'xrp_last_ledger': seen})
        elif failed:
            print(f'[xrp_onchain] {failed}/{len(watched)} calls failed; cursor not advanced')
        return list(found.values()), []
