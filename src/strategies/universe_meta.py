from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Optional

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
    # LAST field, default None: 'etf' | 'fund' | 'adr' | 'stock' | None (unknown).
    # Static attribute — see security_type_from_profile / overlay_security_types.
    security_type: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "TickerMetadata":
        # security_type is read with .get so older rows / frozen artifacts /
        # fixtures that predate it still load; every other field stays strict.
        kw = {f: row[f] for f in cls.__dataclass_fields__ if f != "security_type"}
        kw["security_type"] = row.get("security_type")
        return cls(**kw)


SECURITY_TYPES = ("etf", "fund", "adr", "stock")


def security_type_from_profile(profile) -> Optional[str]:
    """The ONE mapping from a vendor profile dict (data/.cache/fmp_profile.json
    entry) to a security type. Precedence etf > fund > adr > stock.

    Returns None (unknown) for a missing / empty / tombstoned (``_empty``)
    profile, and also for a non-empty profile that carries none of the
    isEtf / isFund / isAdr keys (a legacy entry: absence of the flags is not
    evidence of a plain stock)."""
    if not isinstance(profile, dict) or not profile or profile.get("_empty"):
        return None
    if not any(k in profile for k in ("isEtf", "isFund", "isAdr")):
        return None
    if profile.get("isEtf"):
        return "etf"
    if profile.get("isFund"):
        return "fund"
    if profile.get("isAdr"):
        return "adr"
    return "stock"


def security_types_from_profiles(profiles: Mapping[str, dict]) -> dict:
    """{symbol: type} for every profile that maps to a known type."""
    out = {}
    for sym, prof in (profiles or {}).items():
        t = security_type_from_profile(prof)
        if t is not None:
            out[sym] = t
    return out


def overlay_security_types(rows: Iterable, types: Mapping[str, str]) -> list:
    """History rule (spec §1): security type is a STATIC attribute, so the
    latest known type per symbol is applied to EVERY snapshot row/date
    (an ETF was always an ETF). `rows` are objects with a `.metadata`
    (TickerMetadata) attribute; returns new row objects' metadata replaced in
    place (rows are the resolver's private per-call objects). Symbols absent
    from `types` keep whatever security_type they already carry."""
    from dataclasses import replace
    out = list(rows)
    for r in out:
        t = types.get(r.metadata.symbol)
        if t is not None and r.metadata.security_type != t:
            r.metadata = replace(r.metadata, security_type=t)
    return out
