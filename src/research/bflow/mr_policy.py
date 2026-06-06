"""SP-6 B-flow Phase-1d — MA-reversion entry policy (PRE-REGISTERED).

Spec (BINDING): docs/superpowers/specs/2026-06-06-sp6-bflow-phase1d-mr-policy-design.md
All constants are pre-registered; NEVER tune after first historical run.

Reuses frozen machinery verbatim: flow_features.compute_features,
energy_counterfactual.running_z, oracle cost trio, flow_policy._delta_bps.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.bflow import oracle
from src.research.bflow.flow_features import compute_features, _reindex_valid_frame
from src.research.bflow.energy_counterfactual import running_z

ZETAS = (1.0, 1.5, 2.0)
SCAN_START, SCAN_END = 30, 383          # decision minutes (inclusive)
MIN_NULL_OBS = 30                       # LOSO floor (spec §3)
GUARDRAIL_BPS = 10.0                    # p95-adverse margin (spec §3)
T_PASS = 3.0                            # cell pass bar (spec §3)
LEGS = ("LONG", "SHORT")


def trigger_z(df):
    """Trailing within-session z of vwap_disp_30 — conventions VERBATIM
    (t-inclusive, ddof=1, NaN guards live in running_z)."""
    return running_z(compute_features(df)["vwap_disp_30"])


def delta_vectors(df, p_eod_dump):
    """(G, C) length-390 float arrays. For decision minute m: fill = bar m+1.
    G[m]  = LONG gross bps of fill vw_{m+1} vs the dump.
    C[m]  = spread_bps(bar_{m+1}) − dump-window spread (differential cost).
    NaN where bar m+1 is invalid/absent or the dump is None/NaN."""
    work, _ = _reindex_valid_frame(df)
    G = np.full(390, np.nan)
    C = np.full(390, np.nan)
    p = oracle._f(p_eod_dump) if p_eod_dump is not None else float("nan")
    if not np.isfinite(p):
        return G, C
    dump_spread = oracle.eod_dump_window_spread_bps(df.to_dict("records"))
    if dump_spread is None:
        dump_spread = 0.0
    vw = work["vw"].to_numpy()
    h = work["h"].to_numpy()
    l = work["l"].to_numpy()
    for m in range(389):
        fill = vw[m + 1]
        if not (fill > 0):              # invalid/absent next bar (NaN-safe)
            continue
        G[m] = oracle.gross_bps(p, fill, "LONG")
        # fill-bar spread via the frozen oracle helper (never re-derive)
        entry_spread = oracle.spread_bps(
            {"vw": fill, "h": h[m + 1], "l": l[m + 1]})
        C[m] = entry_spread - dump_spread
    return G, C


def net_legs(G, C):
    """(net_long, net_short) from the shared vectors."""
    return G - C, -G - C


def simulate_pair(df, p_eod_dump, leg, zeta):
    """One (ticker, session, leg, zeta) entry. Spec §1: scan decision minutes
    [30, 383]; trigger = first t with z<=-zeta (LONG) / z>=+zeta (SHORT) AND a
    valid fill bar at t+1 (else VOID -> keep scanning); fill vw_{t+1}; never
    triggered -> forced dump fallback (delta = 0 BY CONSTRUCTION)."""
    z = trigger_z(df)
    G, C = delta_vectors(df, p_eod_dump)
    nl, ns = net_legs(G, C)
    net = nl if leg == "LONG" else ns
    zv = z.reindex(range(390)).to_numpy()
    for t in range(SCAN_START, SCAN_END + 1):
        ztv = zv[t]
        if not np.isfinite(ztv):
            continue
        hit = (ztv <= -zeta) if leg == "LONG" else (ztv >= zeta)
        if not hit:
            continue
        if not np.isfinite(net[t]):     # VOID: invalid fill bar — scan on
            continue
        return {"leg": leg, "zeta": zeta, "triggered": True,
                "entry_minute": t, "delta_net_bps": float(net[t]),
                "gross_at_entry": float(G[t]), "cost_at_entry": float(C[t]),
                "fallback": False}
    return {"leg": leg, "zeta": zeta, "triggered": False,
            "entry_minute": None, "delta_net_bps": 0.0,
            "gross_at_entry": float("nan"), "cost_at_entry": float("nan"),
            "fallback": True}
