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


# ---------------------------------------------------------------------------
# Tier-2: k_eff + floor-preserving block conviction + deflated_net_sharpe
# ---------------------------------------------------------------------------

def k_eff(k: int, rho_bar: float) -> float:
    """Effective number of independent bets among k correlated members. In [1, k]."""
    if k <= 1:
        return 1.0
    rho_bar = max(0.0, min(1.0, rho_bar))
    return k / (1.0 + (k - 1) * rho_bar)


def block_conviction(sharpes: list[float], rho_bar: float) -> float:
    """Floor-preserving deflation: never below the strongest single member.
        conviction = max + (sum - max) * (k_eff - 1)/(k - 1)
    rho_bar->1 => max (one bet); rho_bar->0 => sum (full credit)."""
    k = len(sharpes)
    if k == 0:
        return 0.0
    if k == 1:
        return sharpes[0]
    ke = k_eff(k, rho_bar)
    mx = max(sharpes)
    return mx + (sum(sharpes) - mx) * (ke - 1.0) / (k - 1.0)


def _mean_pairwise(members: list[str], sim: dict[str, dict[str, float]]) -> float:
    if len(members) < 2:
        return 0.0
    vals = []
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            vals.append(sim.get(a, {}).get(b, sim.get(b, {}).get(a, 0.05)))
    return sum(vals) / len(vals) if vals else 0.0


def deflated_net_sharpe(contribs_by_ticker: dict[str, list[tuple]],
                        block_map: dict[str, int],
                        sim: dict[str, dict[str, float]],
                        eff_sharpe: dict[str, float]) -> dict[str, float]:
    """contribs_by_ticker: {ticker: [(strategy_id, direction_int), ...]} (post-fold survivors).
    Returns {ticker: signed deflated net-Sharpe}. Within each (block, direction): floor-preserving
    block_conviction with rho_bar = mean pairwise similarity among that block's firing members.
    Ungrouped strategies are their own singleton block (no deflation). Cross-block = full signed credit."""
    out: dict[str, float] = {}
    singleton_seq = -1
    for ticker, contribs in contribs_by_ticker.items():
        groups: dict[tuple, list[str]] = {}
        local_singleton: dict[str, int] = {}
        for sid, d in contribs:
            bid = block_map.get(sid)
            if bid is None:
                bid = local_singleton.setdefault(sid, singleton_seq)
                singleton_seq -= 1
            groups.setdefault((bid, d), []).append(sid)
        net = 0.0
        for (bid, d), members in groups.items():
            rho = _mean_pairwise(members, sim)
            conv = block_conviction([eff_sharpe.get(s, 0.0) for s in members], rho)
            net += conv * d
        out[ticker] = net
    return out
