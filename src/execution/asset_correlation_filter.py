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


def cap_correlated_clusters(target_usd, conviction, corr, nav,
                            cap_pct=0.22, corr_thr=0.70, single_name_cap_pct=None):
    """Cap each correlated same-direction cluster's gross at cap_pct*nav by keeping
    top-|conviction| names (boundary trimmed to fill) and releasing the rest.
    Pure; gross never increases; no redistribution. Returns (capped, audit)."""
    gross_in = sum(abs(v) for v in target_usd.values())
    base_audit = {'clusters': [], 'total_gross_before': gross_in,
                  'total_gross_after': gross_in, 'released_usd': 0.0}
    if not target_usd or nav <= 0 or not corr:
        return dict(target_usd), base_audit            # INV-5 fail-open
    sign = {t: (1 if v > 0 else -1) for t, v in target_usd.items()}
    cap_usd = cap_pct * nav
    clusters = _cluster_same_direction(list(target_usd), sign, corr, corr_thr)
    out = dict(target_usd)
    audit_clusters = []

    def rank_key(t):
        c = conviction.get(t)
        mag = abs(c) if c is not None else abs(target_usd[t])
        return (mag, abs(target_usd[t]), t)            # deterministic tie-breaks

    for cl in clusters:
        if len(cl) == 1:
            if single_name_cap_pct is None:
                continue                               # singleton, no cluster cap
            eff_cap = single_name_cap_pct * nav
        else:
            eff_cap = cap_usd
        ordered = sorted(cl, key=rank_key, reverse=True)
        gross_before = sum(abs(target_usd[t]) for t in cl)
        kept, trimmed, released = [], [], []
        cum = 0.0
        for t in ordered:
            amt = abs(target_usd[t]); s = sign[t]
            if cum + amt <= eff_cap + 1e-9:
                cum += amt; kept.append((t, out[t]))
            elif cum < eff_cap:
                out[t] = s * (eff_cap - cum)           # boundary partial fill
                trimmed.append((t, target_usd[t], out[t])); cum = eff_cap
            else:
                released.append((t, target_usd[t])); out[t] = 0.0
        audit_clusters.append({'members': cl, 'direction': sign[cl[0]],
                               'gross_before': gross_before, 'gross_after': cum,
                               'kept': kept, 'trimmed': trimmed, 'released': released})
    gross_out = sum(abs(v) for v in out.values())
    return out, {'clusters': audit_clusters, 'total_gross_before': gross_in,
                 'total_gross_after': gross_out, 'released_usd': gross_in - gross_out}
