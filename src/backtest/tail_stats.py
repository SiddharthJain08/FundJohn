"""Advisory sleeve tail statistics (task P3+R3, 2026-08-24 five-repo-adoptions).

ADVISORY ONLY: nothing computed here feeds a gate, sizing, or promotion
decision. Pure numpy, no I/O, no DB — safe to call from anywhere without a
connection or an event loop.

Definitions (per the task brief, verbatim):
  sortino      = mean(r) / downside_dev
  downside_dev = sqrt(mean(min(r, 0)^2))   -- target 0, population form
                 (divides by n, not n-1; this is NOT the annualized,
                 daily-portfolio-equity-curve Sortino already stored in
                 strategy_backtest_regimes.sortino by migration 135 / SP-2
                 Phase C — that is a different, pre-existing metric computed
                 from a portfolio return series. This module's Sortino is a
                 raw per-trade-return ratio and is persisted to a distinctly
                 named column; see migration 148.)
  cvar_5       = mean of the worst floor(alpha * n) observations
                 (None if floor(alpha * n) == 0 -- too few observations in
                 the tail to average)

Edge cases (documented, exercised by tests/backtest/test_tail_stats.py):
  - n < min_obs -> all three outputs None (sample too small to trust).
  - downside_dev == 0 (no observation below the 0 target, e.g. all-positive
    returns) -> sortino is None even when mean(r) > 0, since the ratio is
    undefined (division by zero) rather than infinite-good.
  - floor(alpha * n) == 0 -> cvar_5 is None (the tail slice would be empty).
  - alpha * n is nudged by a tiny epsilon before flooring so that exact
    boundaries (e.g. alpha=0.05, n=20 -> 1.0) aren't lost to a
    floating-point value that lands a hair under the integer (e.g.
    0.9999999999999999) and floors down to the wrong bucket.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

import numpy as np

_FLOOR_EPSILON = 1e-9


def sleeve_tail_stats(pnl_pct: Iterable[float], alpha: float = 0.05,
                       min_obs: int = 20) -> dict:
    """pnl_pct: iterable of per-trade percentage returns (e.g. 1.5 = +1.5%).

    Returns {'sortino': float|None, 'cvar_5': float|None,
             'downside_dev': float|None}.
    """
    r = np.asarray(list(pnl_pct), dtype=float)
    n = r.shape[0]

    if n < min_obs:
        return {'sortino': None, 'cvar_5': None, 'downside_dev': None}

    mean_r = float(r.mean())
    downside = np.minimum(r, 0.0)
    downside_dev = float(math.sqrt(float(np.mean(downside ** 2))))

    sortino: Optional[float]
    if downside_dev == 0.0:
        # Undefined ratio (no downside observed at all in this sleeve) --
        # never reported as "infinitely good", per the brief.
        sortino = None
    else:
        sortino = mean_r / downside_dev

    k = int(math.floor(alpha * n + _FLOOR_EPSILON))
    cvar_5: Optional[float]
    if k == 0:
        cvar_5 = None
    else:
        sorted_r = np.sort(r)
        cvar_5 = float(sorted_r[:k].mean())

    return {'sortino': sortino, 'cvar_5': cvar_5, 'downside_dev': downside_dev}
