#!/usr/bin/env python3
"""Per-regime strategy x strategy similarity + clustering for orthogonalization.

The transpose of correlation_matrix.py (which is ticker-keyed). Lead signal =
holdings co-firing Jaccard over (ISO-week, ticker, direction) emissions; blended
with return-correlation under a data-adaptive weight that rises from 0 as joint
history accrues. Reuses correlation_matrix's clip/sparse conventions.

Spec: docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md
"""
from __future__ import annotations

import os
from typing import Optional

REGIME_STATES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
DEFAULT_WINDOW_DAYS = 90
FOLD_THRESHOLD  = float(os.environ.get('OPENCLAW_FOLD_THRESHOLD', '0.85'))
BLOCK_THRESHOLD = float(os.environ.get('OPENCLAW_BLOCK_THRESHOLD', '0.40'))
RETURN_CORR_ALPHA_CEIL = 0.6     # max weight return-corr ever takes in the blend
ALPHA_FULL_OBS = 60              # overlapping observations at which alpha reaches the ceiling
MAX_OFF_DIAGONAL = 0.95
SPARSE_DEFAULT = 0.05


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    u = len(a | b)
    return (len(a & b) / u) if u else 0.0


def overlap_similarity(sets_by_strat: dict[str, set]) -> dict[str, dict[str, float]]:
    """Pairwise Jaccard over co-firing emission sets. Diagonal 1.0; symmetric."""
    strats = sorted(sets_by_strat.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    for i, a in enumerate(strats):
        out[a][a] = 1.0
        for b in strats[i + 1:]:
            j = jaccard(sets_by_strat[a], sets_by_strat[b])
            out[a][b] = j
            out[b][a] = j
    return out


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    import math
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def return_correlation(returns_by_strat: dict[str, dict[str, float]]
                       ) -> tuple[dict[str, dict[str, float]], dict[tuple, int]]:
    """Pearson on per-strategy {date: daily_return}. Returns (matrix, n_obs_per_pair).
    Sparse / zero-variance pairs default to SPARSE_DEFAULT; off-diagonals clipped +/-0.95."""
    strats = sorted(returns_by_strat.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    n_obs: dict[tuple, int] = {}
    for i, a in enumerate(strats):
        out[a][a] = 1.0
        for b in strats[i + 1:]:
            da, db = returns_by_strat[a], returns_by_strat[b]
            paired = sorted(set(da) & set(db))
            n_obs[(a, b)] = n_obs[(b, a)] = len(paired)
            if len(paired) < 2:
                rho = SPARSE_DEFAULT
            else:
                r = _pearson([da[d] for d in paired], [db[d] for d in paired])
                rho = SPARSE_DEFAULT if r is None else max(-MAX_OFF_DIAGONAL, min(MAX_OFF_DIAGONAL, r))
            out[a][b] = out[b][a] = rho
    return out, n_obs


def adaptive_alpha(n_obs: int) -> float:
    """Weight on return-correlation: 0 at no joint history, rising linearly to the
    ceiling at ALPHA_FULL_OBS overlapping observations, then capped."""
    if n_obs <= 0:
        return 0.0
    return min(RETURN_CORR_ALPHA_CEIL, RETURN_CORR_ALPHA_CEIL * n_obs / ALPHA_FULL_OBS)


def blend_similarity(overlap: dict[str, dict[str, float]],
                     return_corr: dict[str, dict[str, float]],
                     n_obs_per_pair: dict[tuple, int]) -> dict[str, dict[str, float]]:
    """Per-pair convex blend: (1-alpha)*overlap + alpha*return_corr, alpha=adaptive_alpha(n_obs).
    Overlap LEADS; return-corr enters only as joint history accrues. Diagonal 1.0."""
    strats = sorted(overlap.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    for a in strats:
        for b in strats:
            if a == b:
                out[a][b] = 1.0
                continue
            o = overlap.get(a, {}).get(b, 0.0)
            r = return_corr.get(a, {}).get(b, SPARSE_DEFAULT)
            al = adaptive_alpha(n_obs_per_pair.get((a, b), 0))
            out[a][b] = (1.0 - al) * o + al * r
    return out


def cluster_two_cuts(sim: dict[str, dict[str, float]], strategies: list[str],
                     fold_thr: float = FOLD_THRESHOLD, block_thr: float = BLOCK_THRESHOLD
                     ) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Agglomerative average-linkage clustering on distance = 1 - similarity.
    Cut at two heights -> (fold_groups, factor_blocks). Each maps group_id -> [strategy_id].
    <2 strategies -> each its own singleton group."""
    strategies = sorted(strategies)
    n = len(strategies)
    if n < 2:
        groups = {i: [s] for i, s in enumerate(strategies)}
        return dict(groups), dict(groups)

    import numpy as np
    from scipy.cluster.hierarchy import linkage, fcluster
    from scipy.spatial.distance import squareform

    dist = np.zeros((n, n))
    for i, a in enumerate(strategies):
        for k, b in enumerate(strategies):
            if i < k:
                s = max(0.0, min(1.0, sim.get(a, {}).get(b, 0.0)))
                dist[i][k] = dist[k][i] = 1.0 - s
    Z = linkage(squareform(dist, checks=False), method='average')

    def _cut(thr: float) -> dict[int, list[str]]:
        labels = fcluster(Z, t=1.0 - thr, criterion='distance')  # distance cut = 1 - similarity
        groups: dict[int, list[str]] = {}
        for idx, lab in enumerate(labels):
            groups.setdefault(int(lab), []).append(strategies[idx])
        return groups

    return _cut(fold_thr), _cut(block_thr)
