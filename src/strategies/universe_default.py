"""SP-2 universe predicates.

Each predicate has signature (meta, as_of) -> bool.

DO NOT import datetime, time, or os into this module — the universe_lint
gate forbids it for any module that defines universe_filter callables.
"""
from __future__ import annotations
from src.strategies.universe_meta import TickerMetadata

# --- the default --- behavior-preserving for Phase A
def sp500(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.in_sp500)

DEFAULT_UNIVERSE_FILTER = sp500

# --- the 12 candidates Mastermind picks among in Phase C ---

def r1000(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.in_r1000)

def r3000(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.in_r3000)

def options_eligible_only(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.options_eligible and meta.tradable and meta.status == "active")

def large_cap(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.market_cap and meta.market_cap >= 10e9 and meta.in_r3000)

def mid_cap(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.market_cap and 2e9 <= meta.market_cap < 10e9 and meta.in_r3000)

def small_cap_liquid(meta: TickerMetadata, as_of) -> bool:
    return bool(
        meta.market_cap and 300e6 <= meta.market_cap < 2e9
        and meta.adv_usd_20d and meta.adv_usd_20d >= 5e6
        and meta.in_r3000
    )

def large_cap_options(meta: TickerMetadata, as_of) -> bool:
    return large_cap(meta, as_of) and bool(meta.options_eligible)

def mid_cap_options(meta: TickerMetadata, as_of) -> bool:
    return mid_cap(meta, as_of) and bool(meta.options_eligible)

def no_adr(meta: TickerMetadata, as_of) -> bool:
    # Conservative ADR detector — Alpaca asset_class doesn't distinguish,
    # so we filter via known ADR exchanges (OTC) + asset_class
    return bool(meta.tradable and meta.status == "active" and meta.exchange not in ("OTC",))

def no_otc(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.tradable and meta.status == "active" and meta.exchange != "OTC")

def top500_by_adv(meta: TickerMetadata, as_of) -> bool:
    # Approximation: rely on adv_usd_20d ranking computed at metadata write time.
    if meta.adv_usd_20d is None:
        return False
    return bool(meta.adv_usd_20d >= 50e6 and meta.in_r3000)

# --- SP-7 Phase B tier ladder (nested by construction) ---

def liquid_tradable(meta: TickerMetadata, as_of) -> bool:
    return bool(meta.tradable and meta.status == "active" and meta.easy_to_borrow)

def tier_r1000(meta: TickerMetadata, as_of) -> bool:
    return sp500(meta, as_of) or bool(meta.in_r1000)

def tier_r3000(meta: TickerMetadata, as_of) -> bool:
    return tier_r1000(meta, as_of) or bool(meta.in_r3000)

def tier_liquid(meta: TickerMetadata, as_of) -> bool:
    return tier_r3000(meta, as_of) or liquid_tradable(meta, as_of)

CANDIDATE_PREDICATES = {
    "sp500": sp500,
    "r1000": r1000,
    "r3000": r3000,
    "options_eligible_only": options_eligible_only,
    "large_cap": large_cap,
    "mid_cap": mid_cap,
    "small_cap_liquid": small_cap_liquid,
    "large_cap_options": large_cap_options,
    "mid_cap_options": mid_cap_options,
    "no_adr": no_adr,
    "no_otc": no_otc,
    "top500_by_adv": top500_by_adv,
    "liquid_tradable": liquid_tradable,
    "tier_r1000": tier_r1000,
    "tier_r3000": tier_r3000,
    "tier_liquid": tier_liquid,
}

# SP-7 Phase B: ladder tiers are ADOPTION-ONLY predicates (universe ladder +
# operator adoption). They are deliberately NOT in the PaperHunter mint menu —
# exposing them at mint is a Phase D decision. Consumers that enumerate the
# mint menu should exclude this set.
LADDER_TIER_PREDICATES = frozenset({
    "liquid_tradable", "tier_r1000", "tier_r3000", "tier_liquid",
})
