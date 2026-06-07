"""SP-6 B-flow Phase-1f — Intraday Drift Atlas (PRE-REGISTERED, descriptive).

Spec (BINDING): docs/superpowers/specs/2026-06-07-sp6-bflow-phase1f-drift-atlas-prereg.md
Maps the common intraday drift curve G(m) unconditionally — does the curve
deviate from the uniform-accrual null? Purely descriptive; nothing goes live.

Reuses frozen surfaces only:
  mr_policy.delta_vectors   — (G, C) length-390 arrays
  mr_policy._eligible_pair  — dump exists + 60-valid-bar floor
  mr_policy.BUCKETS         — 7 calendar half-year buckets
  oracle.dump_benchmark     — EOD dump price
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.bflow import mr_policy
from src.research.bflow import oracle

# ── pre-registered constants (spec §1-2; NEVER tune) ──────────────────────
N_XS_MIN = 50            # minimum cross-section per minute
MIN_VALID_MINUTES = 300  # minimum valid minutes per session
MIN_SESSIONS = 700       # minimum eligible sessions or INVALID-DATA
T_PASS = 3.0             # adjacency-t bar for TIMING-STRUCTURE

# Pre-named test minutes for multiplicity-controlled verdict (spec §2)
TEST_MINUTES = (5, 15, 30, 60, 120, 180, 240, 300, 330)

_M_TOTAL = 389  # m ∈ [0, 388] (m+1 = fill bar; G[389] always NaN)


# ── session-level curve computation ───────────────────────────────────────

def session_curves(frames, session):
    """One session → curve dict or None if ineligible.

    Per eligible ticker (mr_policy._eligible_pair): G, C = mr_policy.delta_vectors.
    For m in range(0, 389):
      mask = finite G across tickers; n_xs(m) = count
      valid minute iff n_xs ≥ N_XS_MIN
      curve_gross[m] = cross-ticker mean G(m)     (NaN if invalid)
      curve_net[m]   = mean (G−C)(m)              (NaN if invalid)
      cost_curve[m]  = mean C(m)                  (NaN if invalid)
    Session eligible iff valid-minute count ≥ MIN_VALID_MINUTES; else None.
    dev[m] = curve_gross[m] − curve_gross[0]·(389−m)/389
             (NaN where curve_gross[m] or curve_gross[0] is NaN)

    Returns dict with keys:
      session, n_valid_minutes, curve_gross (list, len 389),
      curve_net (list, len 389), cost_curve (list, len 389),
      dev (list, len 389), mean_n_xs
    or None.
    """
    # ── collect (G, C) matrices for eligible tickers ──
    Gs, Cs = [], []
    for ticker in sorted(frames):
        tdf = frames[ticker]
        dump = oracle.dump_benchmark(tdf.to_dict("records"))
        if not mr_policy._eligible_pair(tdf, dump):
            continue
        G, C = mr_policy.delta_vectors(tdf, dump)
        Gs.append(G)
        Cs.append(C)

    if not Gs:
        return None

    G_mat = np.vstack(Gs)   # (n_tickers, 390)
    C_mat = np.vstack(Cs)
    GC_mat = G_mat - C_mat  # net long

    n_tickers = G_mat.shape[0]

    curve_gross = np.full(_M_TOTAL, np.nan)
    curve_net = np.full(_M_TOTAL, np.nan)
    cost_curve_arr = np.full(_M_TOTAL, np.nan)
    n_xs_arr = np.zeros(_M_TOTAL, dtype=int)

    # ── per-minute pooling (m ∈ [0, 388]) ──
    for m in range(_M_TOTAL):
        ok = np.isfinite(G_mat[:, m])   # finite G ⇒ finite C by construction
        n = int(ok.sum())
        n_xs_arr[m] = n
        if n < N_XS_MIN:
            continue
        curve_gross[m] = G_mat[ok, m].mean()
        curve_net[m] = GC_mat[ok, m].mean()
        cost_curve_arr[m] = C_mat[ok, m].mean()

    n_valid = int(np.sum(np.isfinite(curve_gross)))
    if n_valid < MIN_VALID_MINUTES:
        return None

    # ── deviation from uniform-accrual null ──
    # H0: curve(m) = curve(0) · (389-m)/389
    # dev[m] = curve_gross[m] − H0(m)
    # Note: dev[0] ≡ 0 by construction when curve_gross[0] is finite.
    anchor = curve_gross[0]  # may be NaN
    dev = np.full(_M_TOTAL, np.nan)
    for m in range(_M_TOTAL):
        g = curve_gross[m]
        if np.isfinite(g) and np.isfinite(anchor):
            dev[m] = g - anchor * (389 - m) / 389

    mean_n_xs = float(n_xs_arr[n_xs_arr >= N_XS_MIN].mean()) if n_valid > 0 else float("nan")

    return {
        "session": session,
        "n_valid_minutes": n_valid,
        "curve_gross": curve_gross.tolist(),
        "curve_net": curve_net.tolist(),
        "cost_curve": cost_curve_arr.tolist(),
        "dev": dev.tolist(),
        "mean_n_xs": mean_n_xs,
    }


# ── pooled statistics across sessions ─────────────────────────────────────

def pooled_stats(rows):
    """Per-minute across-session mean + clustered t (ddof=1, NaN-skipping) for
    curve_gross, curve_net, cost_curve, dev.

    Returns dict of 4 DataFrames indexed 0..388, each with columns: mean, t, n.
    """
    keys = ("curve_gross", "curve_net", "cost_curve", "dev")
    # Build per-minute session arrays: shape (n_sessions, 389)
    mats = {k: np.array([r[k] for r in rows], dtype=float) for k in keys}

    result = {}
    for k in keys:
        M = mats[k]  # (n_sessions, 389)
        means = np.full(_M_TOTAL, np.nan)
        ts = np.full(_M_TOTAL, np.nan)
        ns = np.zeros(_M_TOTAL, dtype=int)
        for m in range(_M_TOTAL):
            col = M[:, m]
            fin = col[np.isfinite(col)]
            n = len(fin)
            ns[m] = n
            if n == 0:
                continue
            mu = fin.mean()
            means[m] = mu
            if n < 2:
                continue
            sd = fin.std(ddof=1)
            if sd > 0:
                ts[m] = mu / (sd / np.sqrt(n))
        df = pd.DataFrame({"mean": means, "t": ts, "n": ns},
                          index=pd.RangeIndex(_M_TOTAL))
        result[k] = df
    return result


# ── verdict ────────────────────────────────────────────────────────────────

def verdict(dev_stats, n_sessions):
    """Prereg §2 — zero free parameters.

    INVALID-DATA  if n_sessions < MIN_SESSIONS
    TIMING-STRUCTURE  iff ≥2 ADJACENT entries of TEST_MINUTES (adjacent =
                       consecutive in the tuple) have |t(dev)| ≥ 3 and the
                       same sign of mean dev
    FLAT  otherwise
    """
    if n_sessions < MIN_SESSIONS:
        return "INVALID-DATA"

    # Gather (t, sign_of_mean) at each TEST_MINUTES entry
    t_arr = dev_stats["t"].to_numpy()
    mean_arr = dev_stats["mean"].to_numpy()

    # Build list of (t_val, sign) at each test minute in order
    tm_results = []
    for m in TEST_MINUTES:
        t_val = t_arr[m]
        mu = mean_arr[m]
        tm_results.append((m, t_val, mu))

    # Check adjacent pairs (consecutive in TEST_MINUTES tuple)
    for i in range(len(TEST_MINUTES) - 1):
        m0, t0, mu0 = tm_results[i]
        m1, t1, mu1 = tm_results[i + 1]
        if not (np.isfinite(t0) and np.isfinite(t1)):
            continue
        if abs(t0) >= T_PASS and abs(t1) >= T_PASS:
            # same sign of mean dev
            if np.isfinite(mu0) and np.isfinite(mu1):
                if (mu0 > 0 and mu1 > 0) or (mu0 < 0 and mu1 < 0):
                    return "TIMING-STRUCTURE"

    return "FLAT"


# ── named shapes ──────────────────────────────────────────────────────────

def named_shapes(gross_stats, dev_stats):
    """Descriptive scored shapes (prereg §2).

    (i) "shorts_at_open": t(curve_gross) at m=0..5; flag True iff
        significantly NEGATIVE (t ≤ −3 at ≥4 of the 6 minutes).
    (ii) "longs_post_open": argmin of mean curve_gross over m∈[10,45],
         its value, |t(dev)| ≥ 3 flag at that minute, whether deeper
         than curve(5).
    """
    gross_t = gross_stats["t"].to_numpy()
    gross_mean = gross_stats["mean"].to_numpy()
    dev_t = dev_stats["t"].to_numpy()

    # (i) shorts at the open: m ∈ {0..5}
    open_ts = [(m, gross_t[m]) for m in range(6)]
    n_sig_neg = sum(1 for (m, t) in open_ts
                    if np.isfinite(t) and t <= -T_PASS)
    shorts_at_open = {
        "t_values": {m: (float(t) if np.isfinite(t) else None)
                     for m, t in open_ts},
        "significant_negative": n_sig_neg >= 4,
        "n_significant_negative": n_sig_neg,
    }

    # (ii) longs_post_open: argmin of mean curve_gross over m ∈ [10, 45]
    post_open_means = gross_mean[10:46]  # m=10..45 inclusive
    if np.any(np.isfinite(post_open_means)):
        # argmin among finite entries
        finite_mask = np.isfinite(post_open_means)
        local_idx = int(np.argmin(np.where(finite_mask, post_open_means, np.inf)))
        argmin_m = local_idx + 10
        min_val = float(gross_mean[argmin_m])
        dev_sig = bool(np.isfinite(dev_t[argmin_m]) and abs(dev_t[argmin_m]) >= T_PASS)
        deeper_than_5 = (np.isfinite(gross_mean[5]) and min_val < gross_mean[5])
    else:
        argmin_m = None
        min_val = float("nan")
        dev_sig = False
        deeper_than_5 = False

    longs_post_open = {
        "argmin_m": argmin_m,
        "min_value_bps": min_val,
        "dev_t_significant": dev_sig,
        "deeper_than_curve5": deeper_than_5,
        "curve5_value_bps": float(gross_mean[5]) if np.isfinite(gross_mean[5]) else None,
    }

    return {
        "shorts_at_open": shorts_at_open,
        "longs_post_open": longs_post_open,
    }


# ── bucket diagnostics ────────────────────────────────────────────────────

def bucket_curves(rows):
    """Per mr_policy.BUCKETS bucket: mean dev at TEST_MINUTES (diagnostic).

    Returns dict {bucket_name: {m: mean_dev}} for each bucket.
    """
    result = {}
    for name, b0, b1 in mr_policy.BUCKETS:
        sub = [r for r in rows if b0 <= r["session"] <= b1]
        if not sub:
            result[name] = {}
            continue
        bkt = {}
        for m in TEST_MINUTES:
            vals = [r["dev"][m] for r in sub
                    if np.isfinite(r["dev"][m])]
            bkt[m] = float(np.mean(vals)) if vals else float("nan")
        result[name] = bkt
    return result
