"""B1 participation-curve planner (pure). VWAP-volume base modulated by sᵢ-decay
front-loading: slice(t) ∝ profile[t]·exp(-lam·s_i·t). Consumes ONLY ≤9:30 info
(an EXPECTED volume profile), never today's realized volume."""
from __future__ import annotations
import math

RTH_BUCKETS = 13  # 30-min buckets 9:30..16:00
# Market-wide intraday U-shape fallback, 13 buckets. The raw weights encode only the
# U-shape; we normalize at load so U_SHAPE is a proper volume-fraction distribution
# summing to 1.0 (the pure-base plan asserts profile sums to 1). Grounding fix 2026-06-02.
_U_RAW = [0.13, 0.09, 0.07, 0.06, 0.055, 0.05, 0.05,
          0.05, 0.055, 0.06, 0.07, 0.085, 0.135]
U_SHAPE = [u / sum(_U_RAW) for u in _U_RAW]


def expected_volume_profile(history_days):
    """history_days: list of per-day lists of RTH_BUCKETS volumes (trailing N days,
    EXCLUDING the day being planned — causal). Returns a length-RTH_BUCKETS profile
    summing to 1.0; falls back to U_SHAPE when history is empty/degenerate."""
    sums = [0.0] * RTH_BUCKETS
    n = 0
    for day in history_days or []:
        if not day or len(day) != RTH_BUCKETS:
            continue
        tot = sum(day)
        if tot <= 0:
            continue
        for i, v in enumerate(day):
            sums[i] += v / tot
        n += 1
    if n == 0:
        return list(U_SHAPE)
    prof = [s / n for s in sums]
    tot = sum(prof)
    return [p / tot for p in prof] if tot > 0 else list(U_SHAPE)


def plan(signed_qty, s_i, profile, lam):
    """signed_qty: +long/-short. s_i in [0,1]. profile: length-RTH_BUCKETS expected
    fractions. lam>=0. Returns [(bucket, slice_qty)] summing to signed_qty. lam=0 or
    s_i=0 ⇒ pure VWAP base. Pure: only `profile` is used (no realized bars)."""
    k = len(profile)
    w = [profile[t] * math.exp(-lam * s_i * t) for t in range(k)]
    tot = sum(w)
    if tot <= 0:
        w, tot = list(profile), (sum(profile) or 1.0)
    return [(t, signed_qty * w[t] / tot) for t in range(k)]
