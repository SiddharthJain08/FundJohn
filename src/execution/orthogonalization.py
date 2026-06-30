#!/usr/bin/env python3
"""Pure live transforms for strategy orthogonalization, consumed by the sizer.

Tier-1 (fold): collapse same-fold-group / same-direction / same-ticker contributions
to a single representative BEFORE the ticker_w / ticker_net_sharpe sums.
Tier-2 (k_eff): deflate within-factor-block conviction at the GATE only.

Spec: docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md
"""
from __future__ import annotations
import math

SPARSE_DEFAULT = 0.05   # unknown strategy-pair similarity (matches strategy_similarity/correlation_matrix)


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


def corr_adjusted_net_sharpe(contribs_by_ticker: dict[str, list[tuple]],
                             sim: dict[str, dict[str, float]],
                             weight_by_strat: dict[str, float],
                             eps: float = 1e-9) -> tuple[dict[str, float], int]:
    """Signed, correlation-adjusted (Sharpe-weighted) combination Sharpe per ticker.

    contribs_by_ticker: {ticker: [(strategy_id, direction_int), ...]} (post-fold survivors).
    sim:   per-regime strategy x strategy similarity matrix {sid: {sid: rho}}.
    weight_by_strat: the w_i basis (cadence-normalized daily_weight).
    Returns ({ticker: signed S_adj}, n_backstop_fires).

    APPROXIMATE (similarity-proxy): `sim` is a heuristic Jaccard-return-corr blend, not a true
    return-correlation matrix and NOT PSD-guaranteed -> the signed quadratic form q can go <= 0.
    The inert non-PSD backstop then falls back to the diagonal ("assume independent") denominator
    and is counted; it is a NaN guard, NOT a deflating floor.

        num = sum_i  w_i^2 * d_i                              (signed: opposing strategies cancel)
        q   = sum_ij w_i * w_j * d_i * d_j * rho_ij           (rho_ii = 1; missing -> SPARSE_DEFAULT)
        S_adj = num / sqrt(q)            if q >  eps          (no floor; full diversification credit)
              = num / sqrt(sum_i w_i^2)  if q <= eps          (backstop; counted)
    """
    out: dict[str, float] = {}
    n_backstop = 0
    for ticker, contribs in contribs_by_ticker.items():
        rows = []
        for sid, d in contribs:
            w = weight_by_strat.get(sid)
            if w is None or not d:
                continue
            rows.append((sid, int(d), float(w)))
        if not rows:
            continue
        num = sum(w * w * d for (_s, d, w) in rows)
        diag = sum(w * w for (_s, _d, w) in rows)
        q = diag                                  # i == j terms (rho_ii = 1)
        n = len(rows)
        for i in range(n):
            sid_i, d_i, w_i = rows[i]
            a_i = w_i * d_i
            row_i = sim.get(sid_i, {})
            for j in range(i + 1, n):
                sid_j, d_j, w_j = rows[j]
                rho = row_i.get(sid_j)
                if rho is None:
                    rho = sim.get(sid_j, {}).get(sid_i, SPARSE_DEFAULT)
                q += 2.0 * a_i * (w_j * d_j) * float(rho)
        if q > eps:
            den = math.sqrt(q)
        else:
            den = math.sqrt(diag) if diag > 0 else 0.0
            n_backstop += 1
        out[ticker] = (num / den) if den > 0 else 0.0
    return out, n_backstop
