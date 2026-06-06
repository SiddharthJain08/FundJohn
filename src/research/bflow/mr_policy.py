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
from src.research.bflow import predictability as pr
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
    the LOSO null — one record per minute with a finite G (valid fill bar).
    Emits ALL valid fill minutes m in [0, 388] — intentionally WIDER than the
    [SCAN_START, SCAN_END] trigger window (the unconditional null covers every
    minute a policy entry could be matched against)."""
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


# --------------------------------------------------------------------------
# LOSO minute-matched null + guardrail (plan AMENDMENT A1: memory-bounded —
# running sums per (ticker, minute) + fixed-size weighted histograms; value
# lists are NEVER stored)
# --------------------------------------------------------------------------
HIST_LO, HIST_HI, HIST_STEP = -2000.0, 2000.0, 0.1
N_BINS = int(round((HIST_HI - HIST_LO) / HIST_STEP)) + 1   # 40001


def _bin_idx(v):
    """Clamped histogram bin index for a bps value."""
    x = min(max(v, HIST_LO), HIST_HI)
    return int(round((x - HIST_LO) / HIST_STEP))


class NullAccumulator:
    """Streaming LOSO machinery (spec §3, amendment A1).

    - Per-(ticker, minute): running ΣG, ΣC, n → EXACT leave-one-session-out
      mean null per entry.
    - Per cell (leg, zeta): weighted histogram of the matched pool for the
      guardrail p95 (weight = the cell's triggered-entry count at that
      (ticker, minute), built by build_cell_weights from pass-1 rows).
      Quantization ≤ HIST_STEP bps — accepted approximation vs the
      pre-registered 10bps margin."""

    def __init__(self, cell_weights=None):
        self.sum_g = {}
        self.sum_c = {}
        self.n = {}
        self.cell_weights = cell_weights or {}
        self.hist = {cell: np.zeros(N_BINS) for cell in self.cell_weights}

    def add_records(self, recs):
        for r in recs:
            k = (r["ticker"], r["minute"])
            g = r["G"]
            c = r["C"]
            self.sum_g[k] = self.sum_g.get(k, 0.0) + g
            self.sum_c[k] = self.sum_c.get(k, 0.0) + c
            self.n[k] = self.n.get(k, 0) + 1
            for cell, w in self.cell_weights.items():
                wt = w.get(k)
                if not wt:
                    continue
                val = (g - c) if cell[0] == "LONG" else (-g - c)
                self.hist[cell][_bin_idx(val)] += wt

    def loso_null(self, ticker, minute, own_g, own_c, leg):
        """((Σ−own)/(n−1)) leg-signed. None if n−1 < MIN_NULL_OBS."""
        k = (ticker, minute)
        n = self.n.get(k, 0)
        if n - 1 < MIN_NULL_OBS:
            return None
        g = (self.sum_g[k] - own_g) / (n - 1)
        c = (self.sum_c[k] - own_c) / (n - 1)
        return (g - c) if leg == "LONG" else (-g - c)

    def pool_p95_adverse(self, cell, own_values_bps):
        """p95 ADVERSE (= −5th percentile) of the cell's matched pool after
        subtracting one count at each entry's own-value bin (own-session
        exclusion: weight algebra w·n − one-per-entry = w·(n−1), exactly the
        spec §3 pool). None if the cell has no histogram or empty pool."""
        h = self.hist.get(cell)
        if h is None:
            return None
        h = h.copy()
        for v in own_values_bps:
            i = _bin_idx(v)
            if h[i] > 0:
                h[i] -= 1
        total = h.sum()
        if total <= 0:
            return None
        cum = np.cumsum(h)
        # Use (n-1)*q + 1 rank to mirror numpy's linear-interpolation quantile
        # convention (avoids systematic +1-bin bias from plain 0.05*total).
        i = int(np.searchsorted(cum, (total - 1) * 0.05 + 1))
        return -(HIST_LO + i * HIST_STEP)


def build_cell_weights(rows):
    """{(leg, zeta): {(ticker, minute): count}} from pass-1 TRIGGERED rows."""
    out = {(leg, z): {} for leg in LEGS for z in ZETAS}
    for r in rows:
        if not r["triggered"]:
            continue
        w = out[(r["leg"], float(r["zeta"]))]
        k = (r["ticker"], r["entry_minute"])
        w[k] = w.get(k, 0) + 1
    return out


def score_rows(rows, acc):
    """Attach excess_bps per spec §3. Returns (scored_rows, n_excluded).
    Fallback rows: excess = 0 by construction. Triggered rows with a thin
    null (LOSO obs < MIN_NULL_OBS) are EXCLUDED (dropped + counted)."""
    scored, excluded = [], 0
    for r in rows:
        if r["fallback"]:
            scored.append(dict(r, excess_bps=0.0))
            continue
        null = acc.loso_null(r["ticker"], r["entry_minute"],
                             r["gross_at_entry"], r["cost_at_entry"], r["leg"])
        if null is None:
            excluded += 1
            continue
        scored.append(dict(r, excess_bps=r["delta_net_bps"] - null))
    return scored, excluded


def cell_stats(scored_rows):
    """Per (leg, zeta): across-session mean of per-session mean excess and the
    clustered t = mean/(sd/sqrt(n_sessions)) — the registered statistic shape."""
    df = pd.DataFrame(scored_rows)
    out = {}
    if not len(df):
        return out
    for (leg, zeta), cell in df.groupby(["leg", "zeta"]):
        sm = cell.groupby("session")["excess_bps"].mean()
        n = len(sm)
        sd = sm.std(ddof=1)
        t = float(sm.mean() / (sd / np.sqrt(n))) if n >= 2 and sd > 0 else float("nan")
        out[(leg, float(zeta))] = {
            "mean_excess_bps": float(sm.mean()), "t": t, "n_sessions": n,
            "trigger_rate": float(cell["triggered"].mean()),
            "mean_delta_vs_dump_bps": float(
                cell.loc[cell["triggered"], "delta_net_bps"].mean())
            if cell["triggered"].any() else float("nan"),
        }
    return out


def guardrail_stats(scored_rows, acc):
    """Per (leg, zeta): EXACT p95 adverse of triggered policy deltas vs the
    HISTOGRAM p95 adverse of the matched pool (own sessions excluded).
    Call with the FULL pass-1 row set (all triggered entries), NOT the thin-null-filtered scored rows — the spec §3 floor applies to scoring only, and the histogram weights were built from all triggered entries."""
    df = pd.DataFrame([r for r in scored_rows if r["triggered"]])
    out = {}
    if not len(df):
        return out
    for (leg, zeta), cell_rows in df.groupby(["leg", "zeta"]):
        cell = (leg, float(zeta))
        pol = -float(np.quantile(cell_rows["delta_net_bps"], 0.05))
        pl = acc.pool_p95_adverse(cell, cell_rows["delta_net_bps"].tolist())
        out[cell] = {"policy_p95_adverse": pol,
                     "pool_p95_adverse": pl if pl is not None else float("nan")}
    return out


def leg_verdicts(stats, guard):
    """Spec §3: cell passes at t >= +3; leg passes with >=2/3 cells; every
    t-passing cell must satisfy the +10bps relative tail guardrail, else the
    leg is PASS-WITH-TAIL-BREACH (no shadow-lane authorization)."""
    verdicts = {}
    for leg in LEGS:
        passing = [z for z in ZETAS
                   if stats.get((leg, z), {}).get("t", float("nan")) >= T_PASS]
        if len(passing) < 2:
            verdicts[leg] = "FAIL"
            continue
        breach = False
        for z in passing:
            g = guard.get((leg, z), {})
            pol = g.get("policy_p95_adverse", float("nan"))
            pl = g.get("pool_p95_adverse", float("nan"))
            if not (pol <= pl + GUARDRAIL_BPS):
                breach = True
        verdicts[leg] = "PASS-WITH-TAIL-BREACH" if breach else "PASS"
    return verdicts


# 7 calendar buckets — IDENTICAL to scripts/bflow_phase1b_hist_evaluate.py
BUCKETS = (
    ("2023H1", "2023-01-01", "2023-06-30"),
    ("2023H2", "2023-07-01", "2023-12-31"),
    ("2024H1", "2024-01-01", "2024-06-30"),
    ("2024H2", "2024-07-01", "2024-12-31"),
    ("2025H1", "2025-01-01", "2025-06-30"),
    ("2025H2", "2025-07-01", "2025-12-31"),
    ("2026Q1", "2026-01-01", "2026-03-31"),
)


def diagnostics(scored_rows):
    """Spec §3 non-gating diagnostics per (leg, zeta): fallback rate, absolute
    tail P(delta_net < -tol) for tol in {5,10,25} (triggered rows), entry-minute
    deciles, and per-bucket mean session excess."""
    df = pd.DataFrame(scored_rows)
    out = {}
    if not len(df):
        return out
    for (leg, zeta), cell in df.groupby(["leg", "zeta"]):
        trig = cell[cell["triggered"]]
        d = {"fallback_rate": float(cell["fallback"].mean())}
        for tol in (5, 10, 25):
            d[f"p_adverse_{tol}"] = (
                float((trig["delta_net_bps"] < -tol).mean())
                if len(trig) else float("nan"))
        d["entry_minute_deciles"] = (
            [int(q) for q in np.quantile(
                trig["entry_minute"], [0.1, 0.5, 0.9])]
            if len(trig) else [])
        sm = cell.groupby("session")["excess_bps"].mean()
        buckets = {}
        for name, b0, b1 in BUCKETS:
            sub = sm[(sm.index >= b0) & (sm.index <= b1)]
            if len(sub):
                buckets[name] = {"mean_excess_bps": float(sub.mean()),
                                 "n_sessions": int(len(sub))}
        d["buckets"] = buckets
        out[(leg, float(zeta))] = d
    return out
