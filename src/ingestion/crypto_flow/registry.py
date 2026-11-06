"""Exchange-address registry: {chain: {address: owner}} and flow-direction classification."""
from __future__ import annotations

import json
from pathlib import Path

REGISTRY_PATH = Path(__file__).with_name('exchange_addresses.json')
_CASE_SENSITIVE = {'BTC', 'SOL', 'XRP'}


def _norm(chain: str, address: str) -> str:
    return address if chain in _CASE_SENSITIVE else address.lower()


def load_registry(path=REGISTRY_PATH) -> dict[str, dict[str, str]]:
    raw = json.loads(Path(path).read_text())
    return {chain: {_norm(chain, a): owner for owner, addrs in owners.items() for a in addrs}
            for chain, owners in raw.items() if not chain.startswith('_')}


def owner_of(chain: str, address: str | None, reg) -> tuple[str | None, str | None]:
    if not address:
        return None, None
    owner = reg.get(chain, {}).get(_norm(chain, address))
    return (owner, 'exchange') if owner else (None, None)


def classify_direction(from_owner_type, to_owner_type) -> str:
    f, t = from_owner_type == 'exchange', to_owner_type == 'exchange'
    if f and t:
        return 'exchange_to_exchange'
    if t:
        return 'exchange_in'
    if f:
        return 'exchange_out'
    return 'unknown'
