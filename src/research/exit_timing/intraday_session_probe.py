"""Probe ①: exit-day intraday-session return for max_hold-long exits.

Pure-function core (compute_probe) + helpers. The runner (scripts/
run_intraday_session_probe.py) supplies the data. Spec:
docs/archive/superpowers/specs/2026-06-08-sp6-longs-open-exit-probe-design.md
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


def _is_equity(ticker: str) -> bool:
    """Exclude indices (^...), crypto (-USD), fx (=X), pairs (/) from the
    equity universe used for SECONDARY and the M2 same-day baseline."""
    t = str(ticker)
    return not (t.startswith("^") or "-USD" in t or "=" in t or "/" in t)


def prep_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Equity-only rows with a finite intraday_return = (close-open)/open."""
    df = prices[["ticker", "date", "open", "close"]].copy()
    df = df[df["ticker"].map(_is_equity)]
    df = df[df["open"] > 0]
    df["intraday_return"] = (df["close"] - df["open"]) / df["open"]
    df = df[df["intraday_return"].notna()]
    return df[["ticker", "date", "intraday_return"]].reset_index(drop=True)


def attach_regime_bucket(df: pd.DataFrame, regimes: pd.DataFrame) -> pd.DataFrame:
    reg = regimes[["date", "regime"]].copy()
    reg["date"] = reg["date"].astype(str)   # datetime.date -> 'YYYY-MM-DD'
    out = df.merge(reg, on="date", how="left")
    out["bucket"] = out["date"].map(half_year_bucket)
    return out


def attach_primary(primary: pd.DataFrame, prices_prepped: pd.DataFrame) -> pd.DataFrame:
    """Inner-join PRIMARY (ticker,date) to its intraday_return."""
    return primary.merge(prices_prepped, on=["ticker", "date"], how="inner")


def bucket_stats(df: pd.DataFrame, bucket_col: str) -> pd.DataFrame:
    """Per-bucket clustered-t (cluster=date). Returns df indexed by bucket
    with columns mean,t,n, sorted by bucket label."""
    rows = []
    for b, sub in df.groupby(bucket_col):
        if pd.isna(b):
            continue
        mean, t, n = clustered_t(sub, "intraday_return", "date")
        rows.append({bucket_col: b, "mean": mean, "t": t, "n": n})
    if not rows:
        return pd.DataFrame(columns=[bucket_col, "mean", "t", "n"])
    out = pd.DataFrame(rows).sort_values(bucket_col).reset_index(drop=True)
    return out


def _m1(df: pd.DataFrame) -> dict:
    mean, t, n = clustered_t(df, "intraday_return", "date")
    return {"mean": mean, "t": t, "n": n}


def compute_probe(primary: pd.DataFrame, prices: pd.DataFrame,
                  regimes: pd.DataFrame) -> dict:
    """Pure core. Returns all stats + the pre-registered verdict."""
    prepped = prep_prices(prices)

    # PRIMARY (max_hold-long exit days) with returns + regime + bucket
    prim = attach_primary(primary, prepped)
    prim = attach_regime_bucket(prim, regimes)

    # SECONDARY: full equity universe day-by-day
    sec = attach_regime_bucket(prepped, regimes)

    # M2: PRIMARY return minus the same-day equity-universe mean
    uni_day_mean = prepped.groupby("date")["intraday_return"].mean()
    rel = prim.copy()
    rel["intraday_return"] = rel["intraday_return"] - rel["date"].map(uni_day_mean)

    halfyear = bucket_stats(prim, "bucket")
    recent_ts = list(halfyear.sort_values("bucket")["t"].tail(2)) if len(halfyear) else []

    primary_m1 = _m1(prim)
    v = verdict(primary_m1["mean"], primary_m1["t"], recent_ts, primary_m1["n"])

    return {
        "primary_m1": primary_m1,
        "secondary_m1": _m1(sec),
        "m2_relative": _m1(rel),
        "by_regime": bucket_stats(prim, "regime").to_dict("records"),
        "by_halfyear": halfyear.to_dict("records"),
        "recent_ts": recent_ts,
        "verdict": v,
        "n_primary_rows": int(len(prim)),
    }
