"""Data models for the crypto flow engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

MARKET_TICKER = '__MARKET__'


@dataclass
class WhaleEvent:
    ts_utc: datetime
    source: str
    chain: str
    symbol: str
    ticker: str | None
    amount: float
    amount_usd: float | None
    from_addr: str | None
    from_owner: str | None
    from_owner_type: str | None
    to_addr: str | None
    to_owner: str | None
    to_owner_type: str | None
    tx_type: str  # 'transfer' | 'mint' | 'burn'
    tx_hash: str
    out_index: int = 0
    # 'exchange_in' | 'exchange_out' | 'exchange_to_exchange' | 'unknown'
    direction: str = 'unknown'

    def key(self) -> tuple[str, str, str, int]:
        return (self.source, self.chain, self.tx_hash, self.out_index)


@dataclass
class Observation:
    ts_utc: datetime
    ticker: str
    source: str
    fields: dict[str, float | int | str | None]


@dataclass
class ProviderResult:
    provider: str
    ok: bool
    events: list[WhaleEvent]
    observations: list[Observation]
    rows: int
    latency_ms: int
    err: str | None = None
    skipped: str | None = None
