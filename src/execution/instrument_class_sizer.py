"""SP-3 sizing dispatcher. Routes a single sized order by instrument_class.
equity/etp are literal pass-throughs (the live equity path must be byte-identical).
option scales notional by |delta| when a delta is carried on the order (fail-open
to raw notional otherwise — full greeks-driven delta sizing is an SP-3.x fast-follow).
crypto/futures raise (reserved, no handler until SP-3.1).

Executor note (SP-3 Task 8): ETPs are `us_equity` to Alpaca (verified at
alpaca_executor.py `_skip_extended_hours`: it reads the asset `class` field
and only `us_equity` passes), so ETP orders ride the existing equity execution
+ session rails with NO executor branch in the MVP. crypto (24/7) needs
asset-class-aware session logic — deferred to SP-3.1."""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

_PASS_THROUGH = {"equity", "etp"}
_RESERVED = {"crypto", "futures"}


def apply_instrument_class_sizing(order: dict, instrument_class: str) -> dict:
    if instrument_class in _PASS_THROUGH:
        return order
    if instrument_class == "option":
        delta = order.get("delta")
        try:
            d = abs(float(delta)) if delta is not None else None
        except (TypeError, ValueError):
            d = None
        if d is None or d <= 0:
            logger.warning("[instrument_class_sizer] option order %s has no usable "
                           "delta; using raw notional (fail-open)", order.get("ticker"))
            return order
        scaled = dict(order)
        scaled["notional_usd"] = round(float(order["notional_usd"]) * d, 2)
        return scaled
    if instrument_class in _RESERVED:
        raise NotImplementedError(
            f"no sizer handler registered for instrument_class={instrument_class!r} "
            f"(reserved for SP-3.1)")
    raise NotImplementedError(f"unknown instrument_class={instrument_class!r}")
