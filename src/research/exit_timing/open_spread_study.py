"""SP-6 — Open-Window Spread-Cost Study (PRE-REGISTERED).

Pure-function core + helpers for measuring incremental NBBO half-spread
(open 09:31-09:32 ET vs close 15:55-15:56 ET) on recently-dropped max_hold
exits. The runner (scripts/run_open_spread_study.py) handles PG load, CLI
pulls, and output.

Spec: docs/archive/superpowers/specs/2026-06-08-sp6-open-spread-cost-study-prereg.md
"""
from __future__ import annotations

import statistics
from datetime import datetime
from math import ceil
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

# ── pre-registered constants (LOCKED) ────────────────────────────────────────
LONG_GROSS_EDGE_BPS: float = 0.098
SHORT_GROSS_EDGE_BPS: float = 1.940
MIN_QUOTES_PER_WINDOW: int = 3
K_QUOTES: int = 20
SAMPLE_PER_DIR: int = 2000
MIN_VALID_EVENTS: int = 300
SHIP_SHORT_BPS: float = 0.5

_NY = ZoneInfo("America/New_York")


# ── core measurement functions ────────────────────────────────────────────────

def half_spread_bps(ap: float, bp: float) -> Optional[float]:
    """NBBO half-spread in bps.

    Returns None for crossed (ap<=bp), locked (ap==bp), or non-positive quotes.
    Formula: ((ap-bp)/2) / ((ap+bp)/2) * 1e4.
    """
    if ap <= 0 or bp <= 0:
        return None
    if ap <= bp:
        return None
    mid = (ap + bp) / 2.0
    return ((ap - bp) / 2.0) / mid * 1e4


def window_median_half_spread(quotes: list[dict], k: int = K_QUOTES) -> Optional[float]:
    """Median half-spread across the first k valid quotes in a window.

    quotes: list of dicts with 'ap' (ask price) and 'bp' (bid price),
            in arrival order.
    k: maximum number of valid quotes to consume (default K_QUOTES=20).
       Processes quotes IN ORDER, dropping crossed/locked/invalid, and
       collects the first k valid half-spreads.
    Returns None if fewer than MIN_QUOTES_PER_WINDOW valid quotes found.
    """
    valids = []
    for q in quotes:
        if len(valids) >= k:
            break
        hs = half_spread_bps(q.get("ap", 0), q.get("bp", 0))
        if hs is not None:
            valids.append(hs)
    if len(valids) < MIN_QUOTES_PER_WINDOW:
        return None
    return statistics.median(valids)


def deterministic_sample(pairs: list[tuple[str, str]], n: int) -> list[tuple[str, str]]:
    """Deterministic even-stride sample from a sorted list of (ticker, date) pairs.

    Sorts input, then if len<=n returns all. Otherwise takes stride=ceil(len/n)
    and picks indices 0, step, 2*step, … (per prereg §1).
    """
    sorted_pairs = sorted(pairs)
    if len(sorted_pairs) <= n:
        return sorted_pairs
    step = ceil(len(sorted_pairs) / n)
    return [sorted_pairs[i] for i in range(0, len(sorted_pairs), step)]


def et_window_utc(date_str: str, start_hm: tuple[int, int, int],
                  end_hm: tuple[int, int, int]) -> tuple[str, str]:
    """Convert ET time windows to UTC ISO-8601 strings with Z suffix.

    date_str: 'YYYY-MM-DD'
    start_hm, end_hm: (hour, minute, second) in ET
    Returns: (start_iso_z, end_iso_z) e.g. '2025-07-01T13:31:00Z'
    DST handled automatically by ZoneInfo.
    """
    year, month, day = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])

    def _to_utc_z(h: int, m: int, s: int) -> str:
        naive = datetime(year, month, day, h, m, s)
        ny_dt = naive.replace(tzinfo=_NY)
        utc_dt = ny_dt.astimezone(ZoneInfo("UTC"))
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return _to_utc_z(*start_hm), _to_utc_z(*end_hm)


def aggregate(events: list[dict]) -> dict:
    """Compute per-direction distribution of open_hs, close_hs, incr.

    events: list of dicts with keys: ticker, date, direction, open_hs,
            close_hs, incr (only fully-valid events with both windows non-None).

    Returns dict with keys 'long' and 'short', each containing:
        n, open_hs_{mean,median,p25,p75,p90},
        close_hs_{mean,median,p25,p75,p90},
        incr_{mean,median,p25,p75,p90}.
    Returns n=0 with all stats as None when a direction has no events.
    """
    result = {}
    for direction in ("long", "short"):
        subset = [e for e in events if e.get("direction") == direction]
        n = len(subset)
        if n == 0:
            result[direction] = {
                "n": 0,
                "open_hs_mean": None, "open_hs_median": None,
                "open_hs_p25": None, "open_hs_p75": None, "open_hs_p90": None,
                "close_hs_mean": None, "close_hs_median": None,
                "close_hs_p25": None, "close_hs_p75": None, "close_hs_p90": None,
                "incr_mean": None, "incr_median": None,
                "incr_p25": None, "incr_p75": None, "incr_p90": None,
            }
            continue

        def _stats(key: str) -> dict:
            arr = np.array([e[key] for e in subset], dtype=float)
            return {
                f"{key}_mean": float(np.mean(arr)),
                f"{key}_median": float(np.median(arr)),
                f"{key}_p25": float(np.percentile(arr, 25)),
                f"{key}_p75": float(np.percentile(arr, 75)),
                f"{key}_p90": float(np.percentile(arr, 90)),
            }

        entry: dict = {"n": n}
        entry.update(_stats("open_hs"))
        entry.update(_stats("close_hs"))
        entry.update(_stats("incr"))
        result[direction] = entry

    return result


def verdict(agg: dict) -> dict:
    """Pre-registered verdict from §3 of the prereg.

    Reads the aggregated distribution and computes net edge at mean & median.
    label choices (in priority order):
      INVALID-DATA  — either direction has n < MIN_VALID_EVENTS
      PARK          — short_net_mean<=0 AND short_net_median<=0
      SHIP-SHORTS-CANDIDATE — short_net_median > SHIP_SHORT_BPS
      MARGINAL      — otherwise
    """
    long_agg = agg.get("long", {})
    short_agg = agg.get("short", {})

    long_n = long_agg.get("n", 0)
    short_n = short_agg.get("n", 0)

    if long_n < MIN_VALID_EVENTS or short_n < MIN_VALID_EVENTS:
        label = "INVALID-DATA"
        return {
            "long_net_mean": None, "long_net_median": None,
            "short_net_mean": None, "short_net_median": None,
            "label": label,
        }

    long_incr_mean = long_agg["incr_mean"]
    long_incr_median = long_agg["incr_median"]
    short_incr_mean = short_agg["incr_mean"]
    short_incr_median = short_agg["incr_median"]

    long_net_mean = LONG_GROSS_EDGE_BPS - long_incr_mean
    long_net_median = LONG_GROSS_EDGE_BPS - long_incr_median
    short_net_mean = SHORT_GROSS_EDGE_BPS - short_incr_mean
    short_net_median = SHORT_GROSS_EDGE_BPS - short_incr_median

    if short_net_mean <= 0 and short_net_median <= 0:
        label = "PARK"
    elif short_net_median > SHIP_SHORT_BPS:
        label = "SHIP-SHORTS-CANDIDATE"
    else:
        label = "MARGINAL"

    return {
        "long_net_mean": long_net_mean,
        "long_net_median": long_net_median,
        "short_net_mean": short_net_mean,
        "short_net_median": short_net_median,
        "label": label,
    }
