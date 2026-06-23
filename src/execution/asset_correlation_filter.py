"""Per-cluster gross cap on correlated, same-direction assets. Pure.

Clusters same-direction tickers by price-return correlation (average-linkage,
cut at distance 1 - corr_thr), keeps the highest-conviction names until the
cluster's cumulative gross hits cap_pct * NAV (boundary name trimmed to fill),
and RELEASES the rest (target -> 0; never redistributes). Gross is monotonically
non-increasing. Mirrors the SP-6 per-ticker conviction cap (release, no renorm).
"""
from __future__ import annotations


def _cluster_same_direction(tickers, sign, corr, corr_thr):
    """Average-linkage clusters computed separately within each direction.
    Returns list[list[ticker]]; singletons included."""
    import numpy as np
    from scipy.cluster.hierarchy import linkage, fcluster
    from scipy.spatial.distance import squareform
    out = []
    for d in (1, -1):
        grp = sorted([t for t in tickers if sign.get(t) == d])
        n = len(grp)
        if n == 0:
            continue
        if n == 1:
            out.append([grp[0]])
            continue
        dist = np.zeros((n, n))
        for i in range(n):
            for k in range(i + 1, n):
                c = max(-1.0, min(1.0, corr.get(grp[i], {}).get(grp[k], 0.0)))
                dist[i][k] = dist[k][i] = 1.0 - c
        Z = linkage(squareform(dist, checks=False), method='average')
        labels = fcluster(Z, t=1.0 - corr_thr, criterion='distance')
        groups = {}
        for idx, lab in enumerate(labels):
            groups.setdefault(int(lab), []).append(grp[idx])
        out.extend(groups.values())
    return out
