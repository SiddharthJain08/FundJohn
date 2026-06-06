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
    p = oracle._f(p_eod_dump)
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


def _scan(zv, net, G, C, leg, zeta):
    """Scan decision minutes [SCAN_START, SCAN_END] for the first trigger.

    NaN z never triggers. LONG hits at z<=-zeta; SHORT hits at z>=+zeta.
    Non-finite net at t = VOID (invalid fill bar) — continue scanning.
    Returns the triggered row or the fallback row dict.

    NOTE: `entry_minute` is the DECISION minute t (the fill occurs at bar t+1).
    This is distinct from energy_counterfactual.simulate_leg's fill-minute
    convention, where the recorded minute is the bar where the fill executes.
    """
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


def simulate_pair(df, p_eod_dump, leg, zeta):
    """One (ticker, session, leg, zeta) entry. Spec §1: scan decision minutes
    [30, 383]; trigger = first t with z<=-zeta (LONG) / z>=+zeta (SHORT) AND a
    valid fill bar at t+1 (else VOID -> keep scanning); fill vw_{t+1}; never
    triggered -> forced dump fallback (delta = 0 BY CONSTRUCTION).

    Thin wrapper over _scan — z/G/C computed once here; simulate_session_rows
    hoists them for the 6x (leg x zeta) reuse per (ticker, session)."""
    zv = trigger_z(df).to_numpy()
    G, C = delta_vectors(df, p_eod_dump)
    nl, ns = net_legs(G, C)
    net = nl if leg == "LONG" else ns
    return _scan(zv, net, G, C, leg, zeta)


def _eligible_pair(tdf, dump):
    from src.research.bflow import predictability as pr
    if dump is None or not np.isfinite(oracle._f(dump)):
        return False
    return pr._valid_bar_count(tdf) >= pr.MIN_VALID_BARS


def simulate_session_rows(frames, session):
    """Pass-1 worker: all eligible tickers x LEGS x ZETAS for one session.
    Eligibility (spec §2): dump exists + registered 60-valid-bar floor.
    HOISTED HOT PATH (Task-1 quality-review finding): z/G/C are computed ONCE
    per (ticker, session) — 6x redundancy removed. Semantics identical to
    simulate_pair (locked by test_simulate_session_rows_matches_simulate_pair)."""
    rows = []
    for ticker, tdf in frames.items():
        dump = oracle.dump_benchmark(tdf.to_dict("records"))
        if not _eligible_pair(tdf, dump):
            continue
        zv = trigger_z(tdf).to_numpy()
        G, C = delta_vectors(tdf, dump)
        nl, ns = net_legs(G, C)
        for leg in LEGS:
            net = nl if leg == "LONG" else ns
            for zeta in ZETAS:
                row = _scan(zv, net, G, C, leg, zeta)
                row.update({"session": session, "ticker": ticker})
                rows.append(row)
    return rows


def session_delta_records(frames, session):
    """Pass-2 worker: per-(ticker, minute) unconditional entry economics for
    the LOSO null — one record per minute with a finite G (valid fill bar)."""
    recs = []
    for ticker, tdf in frames.items():
        dump = oracle.dump_benchmark(tdf.to_dict("records"))
        if not _eligible_pair(tdf, dump):
            continue
        G, C = delta_vectors(tdf, dump)
        for m in np.flatnonzero(np.isfinite(G)):
            recs.append({"session": session, "ticker": ticker,
                         "minute": int(m), "G": float(G[m]),
                         "C": float(C[m])})
    return recs
