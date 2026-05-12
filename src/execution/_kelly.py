"""Shared Kelly-fraction helpers for sizers.

Extracted from deterministic_sizer.py so multiple sizers (deterministic
and regime_blended) can use the same math without duplicating logic.

Public functions:
  - reward_to_risk(direction, entry, stop, t1) → float | None
  - kelly_fraction(p, R) → float (signed; negative means negative-EV)
  - enrich_with_kelly(signals) → list[dict] (adds clamped kelly_p ≥ 0)

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-revision.md §"Correction 2"
"""
from __future__ import annotations

LONG_DIRECTIONS = ('LONG', 'BUY', 'BUY_VOL')
SHORT_DIRECTIONS = ('SHORT', 'SELL', 'SELL_VOL')


def reward_to_risk(direction: str, entry: float, stop: float, t1: float) -> float | None:
    """Compute R = reward / risk. Returns None for malformed brackets.

    LONG / BUY / BUY_VOL: stop must be < entry < t1; R = (t1 - entry) / (entry - stop)
    SHORT / SELL / SELL_VOL: stop must be > entry > t1; R = (entry - t1) / (stop - entry)
    Also accepts BUY_* and SELL_* prefixes.
    """
    # Accept numeric direction (+1 = LONG, -1 = SHORT) as well as string.
    if direction == 1:
        direction = 'LONG'
    elif direction == -1:
        direction = 'SHORT'
    d = (direction or '').upper()

    # Check if it's a LONG direction (exact match or BUY prefix)
    is_long = d in LONG_DIRECTIONS or d.startswith('BUY')

    if is_long:
        # For LONG: stop < entry < t1
        if entry <= stop:
            return None
        return (t1 - entry) / (entry - stop)

    # Check if it's a SHORT direction
    is_short = d in SHORT_DIRECTIONS or d.startswith('SELL')
    if is_short:
        # For SHORT: t1 < entry < stop
        if stop <= entry:
            return None
        return (entry - t1) / (stop - entry)

    return None


def kelly_fraction(p: float, R: float) -> float:
    """Signed Kelly fraction: f* = (p*R - (1-p)) / R.

    Negative when EV is negative. Caller should clamp ≥ 0 before sizing.
    Returns 0.0 if R <= 0 (degenerate).
    """
    if R <= 0:
        return 0.0
    return (p * R - (1.0 - p)) / R


def enrich_with_kelly(signals: list[dict]) -> list[dict]:
    """Add `kelly_p` to each signal dict (clamped to ≥ 0).

    Accepts both the handoff-style field names (entry, stop, t1) and the
    raw execution_signals column names (entry_price, stop_loss, target_1).
    Missing or malformed brackets get kelly_p = 0.

    Returns NEW list of NEW dicts; input is not mutated.
    """
    out = []
    for sig in signals:
        s = dict(sig)
        direction = s.get('direction', '')
        entry = s.get('entry') if 'entry' in s else s.get('entry_price')
        stop = s.get('stop') if 'stop' in s else s.get('stop_loss')
        t1 = s.get('t1') if 't1' in s else s.get('target_1')
        p_t1 = s.get('p_t1', 0.0) or 0.0

        try:
            entry = float(entry) if entry is not None else None
            stop = float(stop) if stop is not None else None
            t1 = float(t1) if t1 is not None else None
            p_t1 = float(p_t1)
        except (TypeError, ValueError):
            s['kelly_p'] = 0.0
            out.append(s)
            continue

        if entry is None or stop is None or t1 is None or p_t1 <= 0:
            s['kelly_p'] = 0.0
            out.append(s)
            continue

        R = reward_to_risk(direction, entry, stop, t1)
        if R is None or R <= 0:
            s['kelly_p'] = 0.0
            out.append(s)
            continue

        f = kelly_fraction(p_t1, R)
        s['kelly_p'] = max(0.0, f)
        out.append(s)
    return out
