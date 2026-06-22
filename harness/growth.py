"""Growth objective + day-block (stationary-by-day) bootstrap CI on ΔG.

Per the T-DOM methodology (design §6):
  - Per-trade return in σ units: R_i = R_ret_i / sigma_ret_i.
  - Growth (spec §4, log form, φ=0.5):
        G = mean_i[ ln(clip(1 + φ·R_i, 1e-6, None)) ] / mean_i[ τ_i ]
    (lower clip only; 1+φR can be large on the upside.)
  - ΔG CI: block bootstrap by trading DAY (not by trade). The same trades are
    scored under two policies A and B, aligned by index, so A[i] and B[i] share
    a `day`. We draw the distinct-day set with replacement ONCE per resample and
    index BOTH A and B with the SAME concatenated indices (paired bootstrap) —
    this is the only structure that gives the correct ΔG variance and absorbs
    same-day cross-ticker/cross-strategy correlation.
"""
import numpy as np


def growth_G(trades, phi=0.5):
    """Growth G over a list of per-trade dicts {R_ret, tau, sigma_ret}.

    Returns G = mean(ln(clip(1+phi*R, 1e-6, None))) / mean(tau), with
    R = R_ret / sigma_ret. Returns nan on an empty trade set.
    """
    if not trades:
        return float("nan")
    R = np.array([t["R_ret"] / t["sigma_ret"] for t in trades], float)
    tau = np.array([t["tau"] for t in trades], float)
    num = float(np.mean(np.log(np.clip(1.0 + phi * R, 1e-6, None))))
    den = float(np.mean(tau))
    return num / den


def bootstrap_delta(trades_A, trades_B, phi=0.5, n_boot=2000, seed=0):
    """Day-block paired bootstrap of ΔG = G(A) − G(B).

    A and B are the SAME trades under two policies, aligned by index (so
    A[i] and B[i] share `day`). Resample the distinct days with replacement;
    each drawn day contributes ALL its trades (with multiplicity) to both A
    and B via identical indices; recompute G_A − G_B per resample.

    Returns dict(delta, lo, hi, p_gt0) where:
      - delta = G(A) − G(B) on the full sample (point estimate),
      - [lo, hi] = 95% percentile CI of the resampled ΔG,
      - p_gt0 = fraction of resamples with ΔG > 0.
    """
    delta = growth_G(trades_A, phi) - growth_G(trades_B, phi)

    # Day → list of trade indices (built from A; B is index-aligned).
    days = sorted({t["day"] for t in trades_A})
    by_day = {d: [] for d in days}
    for i, t in enumerate(trades_A):
        by_day[t["day"]].append(i)

    rng = np.random.default_rng(seed)
    n_days = len(days)
    deltas = np.empty(n_boot, float)
    for b in range(n_boot):
        drawn = rng.integers(0, n_days, size=n_days)
        idx = [i for j in drawn for i in by_day[days[j]]]
        deltas[b] = (growth_G([trades_A[i] for i in idx], phi)
                     - growth_G([trades_B[i] for i in idx], phi))

    lo, hi = (float(x) for x in np.percentile(deltas, [2.5, 97.5]))
    p_gt0 = float(np.mean(deltas > 0))
    return dict(delta=float(delta), lo=lo, hi=hi, p_gt0=p_gt0)
