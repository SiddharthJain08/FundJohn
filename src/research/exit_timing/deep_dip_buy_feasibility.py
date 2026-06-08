"""SP-6 — Deep-Dip Buy-Leg Feasibility Study (PRE-REGISTERED).

Pure-function core. The runner (scripts/run_deep_dip_buy_feasibility.py)
supplies the data.

Prereg: docs/superpowers/specs/2026-06-08-sp6-deep-dip-buy-feasibility-prereg.md

Isolates the ONE genuinely-measurable, genuinely-untested leg of the operator's
"final structure": a BUY limit 50 bps below the open, resting until 1:30 PM,
market fallback 1:30-3:00 PM.

Unlike the at-touch passive sell (bars over-fill; KILL-only), this is a
DEEP-THROUGH limit: when price falls 50 bps and trades through the bid, the fill
is genuine (the market crosses your level). The over-fill artifact is confined
to the thin touch-and-bounce boundary (low ~= limit), which we measure
(`touch_fraction`). A MEASURED-POSITIVE here is therefore NOT inherently
live-shadow-only.

## Verdict precedence (at DIP_BPS=50):
  INVALID-DATA       n_sessions < MIN_SESSIONS
  KILL               improvement_bps mean <= 0
  MEASURED-POSITIVE  improvement_bps > 0 AND t >= 3 AND touch_fraction < 0.5
  MARGINAL           otherwise (0 < imp but t<3 OR touch_fraction>=0.5)
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

# Reuse the validated bar-read + clustering helpers from the passive study.
from research.exit_timing.passive_window_feasibility import (
    _agg,
    _at,
    clustered_t,
    close_benchmark,
)

# ── pre-registered constants (LOCKED) ─────────────────────────────────────
DIP_LEVELS = (25.0, 50.0, 100.0)   # dip offsets below open, bps (50 = primary)
PRIMARY_DIP = 50.0                 # operator's 0.5%; verdict reads off this
HS_C = 1.0                          # close + PM marketable half-spread, bps
FILL_WIN = (1, 240)                 # 09:31 -> 13:30: limit resting window
PM_WIN = (240, 330)                 # 13:30 -> 15:00 ET: market fallback window
TOUCH_BPS = 2.0                     # touch-boundary band for over-fill diagnostic
DUMP_MIN = 385                      # close benchmark: minutes >= DUMP_MIN
MIN_SESSIONS = 600                  # minimum sessions for a VALID verdict

# re-export for the runner / tests
__all__ = [
    "DIP_LEVELS", "PRIMARY_DIP", "HS_C", "FILL_WIN", "PM_WIN", "TOUCH_BPS",
    "MIN_SESSIONS", "event_deep_dip", "clustered_t", "verdict",
]


def event_deep_dip(tdf: pd.DataFrame, dip_bps: float, hs_c: float) -> Optional[dict]:
    """Compute the deep-dip buy improvement for one (ticker, session).

    Returns None (VOID, not counted) if the open, the close benchmark, or the
    PM fallback window is missing. All improvement values are bps vs the close
    benchmark; positive = bought cheaper than the Phase A close baseline.
    """
    close = close_benchmark(tdf)
    if close is None or close <= 0:
        return None

    open0 = _at(tdf, 0, "o")
    if open0 is None or open0 <= 0:
        return None

    # PM fallback must be executable, else we cannot realize the rule -> VOID.
    pm_vwap = _agg(tdf, PM_WIN[0], PM_WIN[1], "vw", "mean")
    if pm_vwap is None:
        return None

    limit = open0 * (1 - dip_bps / 1e4)
    low_fill = _agg(tdf, FILL_WIN[0], FILL_WIN[1], "l", "min")

    filled = (low_fill is not None) and (low_fill <= limit)

    if filled:
        buy_price = limit
        # touch-boundary: low only grazed the limit -> over-fill-ambiguous.
        touch_boundary = low_fill >= limit * (1 - TOUCH_BPS / 1e4)
    else:
        buy_price = pm_vwap * (1 + hs_c / 1e4)
        touch_boundary = None  # not a fill

    baseline = close * (1 + hs_c / 1e4)
    improvement = (baseline - buy_price) / close * 1e4

    # diagnostic: unconditional open-to-close drift (expected ~0 per the atlas)
    open_to_close = (close - open0) / close * 1e4

    return {
        "improvement": improvement,
        "filled": filled,
        "touch_boundary": touch_boundary,   # bool on fills, None on no-fill
        "open_to_close": open_to_close,
    }


def _fill_rate(rows: list[dict]) -> float:
    if not rows:
        return float("nan")
    return sum(1.0 for r in rows if r.get("filled")) / len(rows)


def _touch_fraction(rows: list[dict]) -> float:
    """Fraction of FILLS that are touch-boundary (over-fill-ambiguous)."""
    fills = [r for r in rows if r.get("filled")]
    if not fills:
        return float("nan")
    return sum(1.0 for r in fills if r.get("touch_boundary")) / len(fills)


def verdict(
    improvement_stats: tuple[float, float, int],
    touch_fraction: float,
    n_sessions: int,
) -> str:
    """Pre-committed verdict at DIP_BPS=50 (prereg §4).

    Precedence: INVALID-DATA -> KILL -> MEASURED-POSITIVE -> MARGINAL.
    """
    if n_sessions < MIN_SESSIONS:
        return "INVALID-DATA"

    mean, t, _ = improvement_stats
    # KILL: the deep-dip buy does not beat the close baseline.
    if mean is None or (isinstance(mean, float) and mean != mean):  # NaN guard
        return "INVALID-DATA"
    if mean <= 0:
        return "KILL"

    # mean > 0 below.
    t_ok = (t == t) and (t >= 3.0)            # t not NaN and significant
    touch_ok = (touch_fraction == touch_fraction) and (touch_fraction < 0.5)
    if t_ok and touch_ok:
        return "MEASURED-POSITIVE"
    return "MARGINAL"
