"""Three rejected/incumbent exit baselines (spec sec.0.2 / sec.7 / sec.12).

Each returns a Policy with the SAME shape as generator.generate (with
diagnostics={}), so the daily multi-day first-touch replay (replay.py) is
policy-agnostic -- only the levels vary across the three.

Policy = dict(
    stop_dist: float,            # absolute price distance |level - entry|
    takes: list[dict(distance: float, fraction: float, time_bars: float)],
    time_stop_bars: float,       # H_max -- baselines have no native time-stop
    direction: int,              # copied from cluster (replay applies the sign)
    diagnostics: dict,           # always {} for baselines
)

All distances are absolute |level - entry| in price units; direction is
handled downstream by the replay engine. Per-take ``time_bars`` is pinned to
H_max so the per-take time trigger can only fire on the horizon-mark bar
(a no-op): baselines have no native per-take time exit. ``weights`` is the
per-leg daily_weight map; it is consumed only by conf_weighted_atr (passed to
the others for signature uniformity).
"""


def _legs(cluster):
    return list(cluster.legs)


def min_stop_cumulative(cluster, weights, H_max=30):
    """Baseline (a): tightest constituent stop; one take per leg at that leg's
    own target_1, each releasing an equal 1/N fraction; no native time-stop."""
    entry = float(cluster.entry)
    legs = _legs(cluster)
    n = len(legs)
    stop_dist = min(abs(entry - float(leg["stop_loss"])) for leg in legs)
    frac = 1.0 / n
    takes = [dict(distance=abs(float(leg["target_1"]) - entry),
                  fraction=frac, time_bars=float(H_max)) for leg in legs]
    return dict(stop_dist=float(stop_dist), takes=takes,
                time_stop_bars=float(H_max), direction=int(cluster.direction),
                diagnostics={})


def conf_weighted_atr(cluster, weights, sharpe, atr, H_max=30):
    """Baseline (b): stop = (sum_i w_i * m_i) * atr where m_i = |entry-stop_i|/atr
    and w_i = daily_weight NORMALIZED; one take (frac 1.0) at the Sharpe-weighted
    mean of constituent |target_1 - entry|; clock-only time-stop -> H_max cap."""
    entry = float(cluster.entry)
    atr = float(atr)
    legs = _legs(cluster)
    ids = [leg["strategy_id"] for leg in legs]

    # daily-weight normalized over the co-firing subset (real clusters won't sum to 1)
    raw_w = [float(weights.get(sid, 0.0)) for sid in ids]
    wsum = sum(raw_w)
    w = [(x / wsum) if wsum > 0 else (1.0 / len(legs)) for x in raw_w]

    # stop: sum_i w_i * m_i * atr; the atr cancels with m_i = |entry-stop_i|/atr
    # so this equals sum_i w_i * |entry - stop_i| -- but compute as specified.
    m = [abs(entry - float(leg["stop_loss"])) / atr for leg in legs]
    stop_dist = sum(wi * mi for wi, mi in zip(w, m)) * atr

    # take: Sharpe-weighted mean of |target_1 - entry|, single tranche
    raw_s = [float(sharpe.get(sid, 0.0)) for sid in ids]
    ssum = sum(raw_s)
    sw = [(x / ssum) if ssum > 0 else (1.0 / len(legs)) for x in raw_s]
    take_dist = sum(swi * abs(float(leg["target_1"]) - entry)
                    for swi, leg in zip(sw, legs))

    takes = [dict(distance=float(take_dist), fraction=1.0, time_bars=float(H_max))]
    return dict(stop_dist=float(stop_dist), takes=takes,
                time_stop_bars=float(H_max), direction=int(cluster.direction),
                diagnostics={})


def current_live_v2(cluster, weights, H_max=30):
    """Informational: current live V2 = tightest constituent stop + a single
    tranche (frac 1.0) at the UNCAPPED sum of constituent |target_1 - entry|."""
    entry = float(cluster.entry)
    legs = _legs(cluster)
    stop_dist = min(abs(entry - float(leg["stop_loss"])) for leg in legs)
    take_dist = sum(abs(float(leg["target_1"]) - entry) for leg in legs)
    takes = [dict(distance=float(take_dist), fraction=1.0, time_bars=float(H_max))]
    return dict(stop_dist=float(stop_dist), takes=takes,
                time_stop_bars=float(H_max), direction=int(cluster.direction),
                diagnostics={})
