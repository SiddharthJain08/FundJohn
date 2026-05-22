from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Optional

@dataclass(frozen=True, slots=True)
class TickerMetadata:
    symbol: str
    asset_class: str
    exchange: Optional[str]
    status: str
    tradable: bool
    shortable: bool
    fractionable: bool
    easy_to_borrow: bool
    market_cap: Optional[float]
    adv_usd_20d: Optional[float]
    sector: Optional[str]
    industry: Optional[str]
    options_eligible: bool
    in_sp500: bool
    in_r1000: bool
    in_r3000: bool
    listed_date: Optional[date]
    delisted_date: Optional[date]

    @classmethod
    def from_row(cls, row: dict) -> "TickerMetadata":
        return cls(**{f: row[f] for f in cls.__dataclass_fields__})
