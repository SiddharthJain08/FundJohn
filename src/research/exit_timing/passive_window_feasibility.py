"""SP-6 — Hybrid Morning-Window Execution Feasibility Study (PRE-REGISTERED).

Pure-function core. The runner (scripts/run_passive_window_feasibility.py)
supplies the data.

Prereg: docs/archive/superpowers/specs/2026-06-08-sp6-passive-window-feasibility-prereg.md

Kill-only: minute bars over-fill passive limits; can't see adverse selection.
A positive sell-side result is INCONCLUSIVE, not a green light.

## Verdict precedence (deliberate):
  INVALID-DATA   n_sessions < MIN_SESSIONS
  INCONCLUSIVE-LEAN-SELL  all_passive combined > 0 OR sell_naive mean > 0
                           (sell-side upper bound exceeds buy drag → live shadow
                            of all-passive warranted; NOT a go-live signal)
  PARK           hybrid combined <= 0  (buy drag dominates; hybrid closed)

Precedence: INCONCLUSIVE-LEAN-SELL checked BEFORE PARK so that the typical
expected outcome (hybrid<=0, sell_naive>0 from bar noise) is correctly labelled
as INCONCLUSIVE rather than silently buried as PARK.  PARK is only reached when
ALL three signals are <= 0 (all_passive combined, hybrid combined, sell_naive).
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

# ── pre-registered constants (LOCKED) ─────────────────────────────────────
HS_LEVELS = (2.0, 5.0, 8.0)   # half-spread levels to test, bps
HS_CLOSE = 1.0                 # close half-spread assumption, bps
SELL_WIN = (5, 10)             # morning sell window: minutes 5..10
SELL_PM_WIN = (360, 389)       # afternoon sell retry window: minutes 360..389
BUY_WIN = (15, 30)             # buy window: minutes 15..30
DUMP_MIN = 385                 # close benchmark: minutes >= 385
MIN_SESSIONS = 600             # minimum sessions for VALID result


# ── helpers ───────────────────────────────────────────────────────────────

def close_benchmark(tdf: pd.DataFrame) -> Optional[float]:
    """Volume-weighted mean of vw over dump minutes (>= DUMP_MIN).

    Returns None if no such minutes exist or total volume is zero.
    """
    sub = tdf[tdf["minute"] >= DUMP_MIN]
    if sub.empty:
        return None
    total_v = float(sub["v"].sum())
    if total_v <= 0:
        return None
    return float((sub["vw"] * sub["v"]).sum() / total_v)


def _at(tdf: pd.DataFrame, minute: int, col: str) -> Optional[float]:
    """Return the value of col at that exact minute, or None if absent."""
    row = tdf[tdf["minute"] == minute]
    if row.empty:
        return None
    return float(row[col].iloc[0])


def _agg(tdf: pd.DataFrame, lo: int, hi: int, col: str, how: str) -> Optional[float]:
    """Aggregate col over minutes in [lo, hi] inclusive.

    how: 'max', 'min', or 'mean'
    Returns None if no minutes in range are present.
    """
    sub = tdf[(tdf["minute"] >= lo) & (tdf["minute"] <= hi)]
    if sub.empty:
        return None
    if how == "max":
        return float(sub[col].max())
    if how == "min":
        return float(sub[col].min())
    if how == "mean":
        return float(sub[col].mean())
    raise ValueError(f"Unknown aggregation: {how}")


def _last_vw_in_window(tdf: pd.DataFrame, lo: int, hi: int) -> Optional[float]:
    """Last available vw in window [lo, hi] by minute order.

    Used as the forced-fill fallback for buy-passive when minute 30 absent.
    """
    sub = tdf[(tdf["minute"] >= lo) & (tdf["minute"] <= hi)]
    if sub.empty:
        return None
    return float(sub.sort_values("minute")["vw"].iloc[-1])


# ── main per-event computation ─────────────────────────────────────────────

def event_improvements(
    tdf: pd.DataFrame,
    hs: float,
    hs_c: float,
) -> Optional[dict]:
    """Compute all improvement metrics for one (ticker, session).

    Returns None if close is missing OR if vw at minute 5 or 15 is absent.
    All improvement values are in bps vs the close benchmark.
    """
    close = close_benchmark(tdf)
    if close is None or close <= 0:
        return None

    mid5 = _at(tdf, 5, "vw")
    mid15 = _at(tdf, 15, "vw")
    if mid5 is None or mid15 is None:
        return None

    # ── SELL — passive naive ───────────────────────────────────────────
    ask_m = mid5 * (1 + hs / 1e4)
    morning_high = _agg(tdf, SELL_WIN[0], SELL_WIN[1], "h", "max")
    sell_fill_morning = (morning_high is not None) and (morning_high >= ask_m)

    if sell_fill_morning:
        sell_price = ask_m
        sell_fill_afternoon = False
    else:
        mid360 = _at(tdf, 360, "vw")
        if mid360 is not None:
            ask_pm = mid360 * (1 + hs / 1e4)
            afternoon_high = _agg(tdf, SELL_PM_WIN[0], SELL_PM_WIN[1], "h", "max")
            sell_fill_afternoon = (afternoon_high is not None) and (afternoon_high >= ask_pm)
        else:
            sell_fill_afternoon = False

        if sell_fill_afternoon:
            # mid360 is guaranteed non-None here
            sell_price = mid360 * (1 + hs / 1e4)  # type: ignore[possibly-undefined]
        else:
            sell_price = None  # no fill → forced MOC

    baseline_sell = close * (1 - hs_c / 1e4)
    if sell_price is not None:
        sell_naive = (sell_price - baseline_sell) / close * 1e4
    else:
        sell_naive = 0.0  # forced MOC = baseline → zero improvement

    # ── SELL — oracle (diagnostic upper bound) ─────────────────────────
    morning_h = _agg(tdf, SELL_WIN[0], SELL_WIN[1], "h", "max")
    afternoon_h = _agg(tdf, SELL_PM_WIN[0], SELL_PM_WIN[1], "h", "max")
    # Guard None before max — early close may have no afternoon minutes
    candidates = [x for x in (morning_h, afternoon_h) if x is not None]
    best_high = max(candidates) if candidates else (morning_h or 0.0)
    sell_oracle = (best_high - baseline_sell) / close * 1e4

    # ── BUY — marketable naive ─────────────────────────────────────────
    vwap_buy = _agg(tdf, BUY_WIN[0], BUY_WIN[1], "vw", "mean")
    min_buy = _agg(tdf, BUY_WIN[0], BUY_WIN[1], "vw", "min")
    baseline_buy = close * (1 + hs_c / 1e4)

    if vwap_buy is not None:
        buy_mkt_naive = (baseline_buy - vwap_buy * (1 + hs / 1e4)) / close * 1e4
    else:
        buy_mkt_naive = 0.0

    # ── BUY — marketable oracle ────────────────────────────────────────
    if min_buy is not None:
        buy_mkt_oracle = (baseline_buy - min_buy * (1 + hs / 1e4)) / close * 1e4
    else:
        buy_mkt_oracle = 0.0

    # ── BUY — passive naive ────────────────────────────────────────────
    bid = mid15 * (1 - hs / 1e4)
    low_in_win = _agg(tdf, BUY_WIN[0], BUY_WIN[1], "l", "min")
    buy_pass_fill = (low_in_win is not None) and (low_in_win <= bid)

    if buy_pass_fill:
        buy_cost = bid
    else:
        # forced market at 10:00 (minute 30); if absent, use last available vw in window
        forced_vw = _at(tdf, 30, "vw")
        if forced_vw is None:
            forced_vw = _last_vw_in_window(tdf, BUY_WIN[0], BUY_WIN[1])
        if forced_vw is None:
            forced_vw = vwap_buy  # last fallback — whole window mean
        buy_cost = (forced_vw or 0.0) * (1 + hs / 1e4)

    buy_pass_naive = (baseline_buy - buy_cost) / close * 1e4

    # ── COMBINED views ─────────────────────────────────────────────────
    hybrid = sell_naive + buy_mkt_naive
    all_passive = sell_naive + buy_pass_naive

    return {
        "sell_naive": sell_naive,
        "sell_oracle": sell_oracle,
        "sell_fill_morning": sell_fill_morning,
        "sell_fill_afternoon": sell_fill_afternoon,
        "buy_mkt_naive": buy_mkt_naive,
        "buy_mkt_oracle": buy_mkt_oracle,
        "buy_pass_naive": buy_pass_naive,
        "buy_pass_fill": buy_pass_fill,
        "combined_hybrid": hybrid,
        "combined_all_passive": all_passive,
    }


# ── statistics ────────────────────────────────────────────────────────────

def clustered_t(rows: list[dict], key: str) -> tuple[float, float, int]:
    """Day-clustered t: per-session mean -> across-session mean & t.

    rows: list of dicts each with 'session' and the key field.
    Returns (mean, t, n_sessions). t is NaN when n<2 or sd==0.
    Convention matches intraday_session_probe.clustered_t.
    """
    if not rows:
        return float("nan"), float("nan"), 0

    # group by session
    session_vals: dict[str, list[float]] = {}
    for r in rows:
        s = r["session"]
        v = r[key]
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        session_vals.setdefault(s, []).append(float(v))

    session_means = [sum(vs) / len(vs) for vs in session_vals.values()]
    n = len(session_means)
    if n == 0:
        return float("nan"), float("nan"), 0

    mean = sum(session_means) / n
    if n < 2:
        return mean, float("nan"), n

    sd = math.sqrt(sum((x - mean) ** 2 for x in session_means) / (n - 1))
    if sd == 0 or math.isnan(sd):
        return mean, float("nan"), n

    t = mean / (sd / math.sqrt(n))
    return mean, t, n


# ── verdict ───────────────────────────────────────────────────────────────

def verdict(
    combined_hybrid_stats: tuple[float, float, int],
    combined_allpassive_stats: tuple[float, float, int],
    sell_naive_stats: tuple[float, float, int],
    n_sessions: int,
) -> str:
    """Pre-committed verdict at the realistic hs=5 level (prereg §3).

    Verdict precedence (deliberate order — see module docstring):
      1. INVALID-DATA   iff n_sessions < MIN_SESSIONS
      2. INCONCLUSIVE-LEAN-SELL iff all_passive combined > 0 OR sell_naive > 0
      3. PARK           otherwise (hybrid combined <= 0, buy drag dominates)

    INCONCLUSIVE is checked BEFORE PARK so that the typical expected outcome
    (hybrid<=0 due to buy drag, sell_naive>0 from bar-noise over-fill) is
    labelled INCONCLUSIVE rather than silently collapsed into PARK.
    PARK is only reached when ALL signals (all_passive, hybrid, sell_naive) <= 0.
    """
    if n_sessions < MIN_SESSIONS:
        return "INVALID-DATA"

    hybrid_mean = combined_hybrid_stats[0]
    allpassive_mean = combined_allpassive_stats[0]
    sell_mean = sell_naive_stats[0]

    # INCONCLUSIVE-LEAN-SELL: sell upper bound exceeds buy drag
    if allpassive_mean > 0 or sell_mean > 0:
        return "INCONCLUSIVE-LEAN-SELL"

    # PARK: buy drag dominates; hybrid closed (hybrid <= 0 AND all_passive <= 0)
    return "PARK"
