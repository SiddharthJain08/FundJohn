"""Probe ①: exit-day intraday-session return for max_hold-long exits.

Pure-function core (compute_probe) + helpers. The runner (scripts/
run_intraday_session_probe.py) supplies the data. Spec:
docs/superpowers/specs/2026-06-08-sp6-longs-open-exit-probe-design.md
"""
from __future__ import annotations

import math
import pandas as pd

# ── pre-registered constants (LOCKED) ─────────────────────────────────
T_VETO = 3.0          # pooled positive t that vetoes
T_RECENT = 2.0        # recent-bucket positive t that vetoes
MIN_CLUSTERS = 500    # min distinct exit-day clusters or INVALID-DATA
REGIMES = ("LOW_VOL", "TRANSITIONING", "HIGH_VOL", "CRISIS")


def clustered_t(df: pd.DataFrame, value_col: str, cluster_col: str):
    """Day-clustered t: per-cluster mean -> across-cluster mean & t.

    Returns (mean, t, n_clusters). t is NaN when n<2 or sd==0.
    """
    g = df.groupby(cluster_col)[value_col].mean()
    n = int(g.shape[0])
    mean = float(g.mean())
    if n < 2:
        return mean, float("nan"), n
    sd = float(g.std(ddof=1))
    if sd == 0 or math.isnan(sd):
        return mean, float("nan"), n
    t = mean / (sd / math.sqrt(n))
    return mean, t, n


def half_year_bucket(date_str: str) -> str:
    """'YYYY-MM-DD' -> 'YYYYH1'|'YYYYH2'."""
    year = date_str[:4]
    month = int(date_str[5:7])
    return f"{year}H1" if month <= 6 else f"{year}H2"


def verdict(primary_mean: float, primary_t: float,
            recent_ts: list[float], n_clusters: int) -> str:
    """Asymmetric veto (spec §1.4). NaN t's are treated as non-significant."""
    if n_clusters < MIN_CLUSTERS:
        return "INVALID-DATA"

    def _sig_pos(t):
        return (t == t) and t >= T_VETO  # t==t filters NaN

    def _sig_pos_recent(t):
        return (t == t) and t >= T_RECENT

    if _sig_pos(primary_t):
        return "NO-GO"
    if any(_sig_pos_recent(t) for t in recent_ts):
        return "NO-GO"
    if primary_mean > 0:
        return "CLEAR-WITH-CAUTION"
    return "CLEAR-TO-SHIP-GATED"
