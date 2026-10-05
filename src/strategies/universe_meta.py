from __future__ import annotations
import re
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


SECURITY_TYPES = ("etf", "fund", "spac", "deriv", "pref", "cef", "adr", "stock")

# Known NON-common-stock types: the single source for "not an operating-company
# share". The Russell-flag ranking (pipeline.backfillers.universe_metadata.
# rank_in_r1000_r3000) drops these from its pool. 'stock', 'adr' and unknown
# (None) are NOT in this set.
NON_COMMON_SECURITY_TYPES = frozenset({"etf", "fund", "spac", "deriv", "pref", "cef"})

# companyName patterns (word-boundary, case-insensitive). Conservative by design:
# a real operating company must stay 'stock' — e.g. "Unity Software", "United
# Rentals", "Rightmove", "Preferred Bank" (bare words never match).
_DERIV_RE = re.compile(r"\b(Warrants?|Units?|Rights?)\b", re.I)
_PREF_RE = re.compile(
    r"\bPreferred\s+(Stock|Shares?|Securities|Units?)\b"
    r"|\bTrust\s+Preferred\b|\bPfd\b"
    r"|\bNotes?\s+due\b|\bSenior\s+Notes?\b|\bSubordinated\b|\bDebentures?\b"
    r"|\bJR\s?SUB\b|\bNTS\b"
    r"|\bSeries\s+[A-Z0-9]+\b.*\bPreferred\b"
    r"|\d\s*%",
    re.I)
_CEF_RE = re.compile(
    r"\bFunds?\b|\bMunicipal\b|\bClosed[- ]End\b|\bLending\b|\bBDC\b"
    r"|\b(Capital|Finance|Investment)\s+Corp(oration)?\.?$",
    re.I)


def security_type_from_profile(profile) -> Optional[str]:
    """The ONE mapping from a vendor profile dict (data/.cache/fmp_profile.json
    entry) to a security type. Precedence, top to bottom:

      isEtf -> 'etf'; isFund -> 'fund';
      industry == 'Shell Companies' -> 'spac';
      companyName warrant/unit/right phrasing -> 'deriv';
      companyName preferred/notes/debenture/subordinated/'%' phrasing -> 'pref';
      industry startswith 'Asset Management' AND closed-end/BDC naming -> 'cef';
      isAdr -> 'adr'; else 'stock'.

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
    industry = profile.get("industry") or ""
    name = profile.get("companyName") or ""
    if industry == "Shell Companies":
        return "spac"
    if _DERIV_RE.search(name):
        return "deriv"
    if _PREF_RE.search(name):
        return "pref"
    if industry.startswith("Asset Management") and _CEF_RE.search(name):
        return "cef"
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
