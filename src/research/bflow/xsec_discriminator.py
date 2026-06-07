"""SP-6 B-flow Phase-1e — cross-sectional discriminator (PRE-REGISTERED).

Spec (BINDING): docs/superpowers/specs/2026-06-07-sp6-bflow-phase1e-xsec-discriminator-prereg.md
Dollar-neutral within-minute decile L/S on vwap_disp_30, measured to the
dump, gross and net of the differential spread cost. Discriminates
cross-sectional edge (a) from common time-of-day drift (b) — an L/S formed
within the same minute differences out ALL common structure by construction.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.bflow import mr_policy
from src.research.bflow import oracle
from src.research.bflow.flow_features import compute_features

N_XS_MIN = 50            # minimum cross-section per minute (prereg §1)
MIN_ELIGIBLE_MINUTES = 100  # minimum eligible minutes per session (prereg §1)
DECILE_FRAC = 0.10
T_PASS = 3.0
MIN_SESSIONS = 700


def session_spreads(frames, session):
    """One session -> dict(session, n_minutes, spread_net, spread_gross,
    long_excess_gross, short_excess_gross, mean_n_xs) or None if ineligible.

    Determinism: tickers processed in sorted order; decile ties broken by
    that order via a STABLE argsort (prereg §1: lexicographic)."""
    disps, Gs, Cs = [], [], []
    for ticker in sorted(frames):
        tdf = frames[ticker]
        dump = oracle.dump_benchmark(tdf.to_dict("records"))
        if not mr_policy._eligible_pair(tdf, dump):
            continue
        disps.append(compute_features(tdf)["vwap_disp_30"].to_numpy())
        G, C = mr_policy.delta_vectors(tdf, dump)
        Gs.append(G)
        Cs.append(C)
    if not disps:
        return None
    D = np.vstack(disps)            # (n_tickers, 390)
    G = np.vstack(Gs)
    C = np.vstack(Cs)
    net_l = G - C
    net_s = -G - C
    sn, sg, lx, sx, nxs = [], [], [], [], []
    for t in range(mr_policy.SCAN_START, mr_policy.SCAN_END + 1):
        ok = np.isfinite(D[:, t]) & np.isfinite(G[:, t])
        n = int(ok.sum())
        if n < N_XS_MIN:
            continue
        k = int(np.floor(DECILE_FRAC * n))
        idx = np.flatnonzero(ok)
        order = idx[np.argsort(D[idx, t], kind="stable")]
        lo = order[:k]               # deepest below VWAP -> LONG leg
        hi = order[-k:]              # furthest above -> SHORT leg
        sn.append(net_l[lo, t].mean() + net_s[hi, t].mean())
        sg.append(G[lo, t].mean() - G[hi, t].mean())
        xs_mean = G[idx, t].mean()   # cross-sectional mean (diagnostic)
        lx.append(G[lo, t].mean() - xs_mean)
        sx.append(xs_mean - G[hi, t].mean())
        nxs.append(n)
    if len(sn) < MIN_ELIGIBLE_MINUTES:
        return None
    return {"session": session, "n_minutes": len(sn),
            "spread_net": float(np.mean(sn)),
            "spread_gross": float(np.mean(sg)),
            "long_excess_gross": float(np.mean(lx)),
            "short_excess_gross": float(np.mean(sx)),
            "mean_n_xs": float(np.mean(nxs))}


def clustered_t(values):
    """mean/(sd/sqrt(n)), sample sd ddof=1; NaN if n<2 or sd==0."""
    v = pd.Series(values, dtype=float)
    n = len(v)
    sd = v.std(ddof=1)
    if n < 2 or not (sd > 0):
        return float("nan")
    return float(v.mean() / (sd / np.sqrt(n)))


def verdict(gross_t, net_t, n_sessions):
    """Prereg §2 — zero free parameters."""
    if n_sessions < MIN_SESSIONS:
        return "INVALID-DATA"
    if not np.isfinite(gross_t):
        return "INVALID-DATA"
    if gross_t <= -T_PASS:
        return "INVERTED"
    if gross_t < T_PASS:
        return "NULL"
    if np.isfinite(net_t) and net_t >= T_PASS:
        return "ECON-PASS"
    return "SIGNAL-ONLY"


def bucket_table(rows):
    """Per-bucket mean spread_net/spread_gross + n (diagnostic, prereg §2)."""
    df = pd.DataFrame(rows)
    out = {}
    if not len(df):
        return out
    for name, b0, b1 in mr_policy.BUCKETS:
        sub = df[(df["session"] >= b0) & (df["session"] <= b1)]
        if len(sub):
            out[name] = {"net": float(sub["spread_net"].mean()),
                         "gross": float(sub["spread_gross"].mean()),
                         "n": int(len(sub))}
    return out
