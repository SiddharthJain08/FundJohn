#!/usr/bin/env python3
"""Pure live transforms for strategy orthogonalization, consumed by the sizer.

Tier-1 (fold): collapse same-fold-group / same-direction / same-ticker contributions
to a single representative BEFORE the ticker_w / ticker_net_sharpe sums.
Tier-2 (k_eff): deflate within-factor-block conviction at the GATE only.

Spec: docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md
"""
from __future__ import annotations


def _dir_to_int(direction) -> int:
    """Map a Signal.direction to {+1, -1, 0}, matching the sizer's _DIR_MAP
    (LONG/BUY/BUY_VOL -> +1, SHORT/SELL/SELL_VOL -> -1, FLAT/unknown -> 0)."""
    d = str(direction or '').upper()
    if d in ('LONG', 'BUY', 'BUY_VOL'):
        return 1
    if d in ('SHORT', 'SELL', 'SELL_VOL'):
        return -1
    if d == 'FLAT':
        return 0
    # Defensive fallback for unanticipated variants
    if d.startswith('L'):
        return 1
    if d.startswith('S'):
        return -1
    return 0


def fold_active_contributions(active: list[dict], fold_map: dict[str, int],
                              rep_map: dict[int, str], eff_sharpe: dict[str, float]) -> list[dict]:
    """For each (ticker, direction, fold_group) bucket of grouped contributions, keep ONE:
    the representative if it fired, else the highest-effective_sharpe member that fired.
    Ungrouped (singleton) contributions pass through untouched."""
    kept: list[dict] = []
    buckets: dict[tuple, list[dict]] = {}
    for s in active:
        sid = s.get('strategy_id')
        gid = fold_map.get(sid)
        if gid is None:
            kept.append(s)                                  # ungrouped -> keep
            continue
        key = (s.get('ticker'), _dir_to_int(s.get('direction')), gid)
        buckets.setdefault(key, []).append(s)
    for (ticker, d, gid), members in buckets.items():
        rep = rep_map.get(gid)
        chosen = next((m for m in members if m.get('strategy_id') == rep), None)
        if chosen is None:
            chosen = max(members, key=lambda m: eff_sharpe.get(m.get('strategy_id'), float('-inf')))
        kept.append(chosen)
    return kept
