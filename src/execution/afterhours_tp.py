#!/usr/bin/env python3
"""afterhours_tp.py — W3: resting extended-hours take-profits.

Alpaca extended-hours orders must be limit + day TIF + extended_hours=true.
A sell-limit above market (buy-limit below, for shorts) is a clean ext-hours
take-profit. Stops cannot be represented in ext-hours, so this covers UPSIDE
only; the RTH GTC stop still covers downside.

Placed at each ext-hours session open; a session-boundary reconcile cancels the
prior session's TP and resizes the (unlinked) GTC stop after an ext-hours fill.

Gate: OPENCLAW_AFTERHOURS_TP (default OFF).
"""
from __future__ import annotations
import os


def afterhours_tp_on() -> bool:
    return os.environ.get('OPENCLAW_AFTERHOURS_TP') == '1'


def desired_tps(positions, bracket_lookup, tp_covered=None):
    """Pure: map open positions to the ext-hours TP orders to place.

    positions: [{'symbol','side','qty'}]
    bracket_lookup(symbol, side) -> {'target': float} | None  (latest placed TP)
    tp_covered: {symbol: qty already resting on a limit} (skip covered qty)
    Returns [{'ticker','side','qty','tp'}] — side is the EXIT side.
    """
    tp_covered = tp_covered or {}
    out = []
    for p in positions:
        sym = p.get('symbol')
        side = (p.get('side') or '').lower()
        try:
            qty = abs(float(p.get('qty') or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if not sym or qty <= 0 or side not in ('long', 'short'):
            continue
        if tp_covered.get(sym, 0.0) >= qty - 0.01:
            continue
        b = bracket_lookup(sym, side)
        tp = (b or {}).get('target')
        if not tp or float(tp) <= 0:
            continue
        exit_side = 'sell' if side == 'long' else 'buy'
        out.append({'ticker': sym, 'side': exit_side, 'qty': int(qty),
                    'tp': float(tp)})
    return out
