"""Pure rule-based panic-score engine for the pre-market sentiment scan.

Inputs are *already-aggregated* sentiment features for a single ticker over
a single pre-market window (typically prior 18:00 ET -> now). Zero I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreInputs:
    news_count_window: int
    news_finbert_neg_ratio: float       # 0..1
    news_finbert_mean_score: float      # -1..1, currently informational only
    social_post_count_window: int
    social_bear_ratio: float            # 0..1


def _safe_unit(x: float) -> float:
    """Clamp to [0, 1]; NaN -> 0."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


def panic_score(inp: ScoreInputs) -> float:
    """Composite 0..100 panic score.

    Hard precondition: news_count_window >= 1, otherwise returns 0.
    (Pure-social signals are a documented follow-up, not MVP.)
    """
    if inp.news_count_window < 1:
        return 0.0

    news_component   = 60.0 * _safe_unit(inp.news_finbert_neg_ratio)
    volume_component = 30.0 * (min(inp.news_count_window * 10, 100) / 100.0)
    social_component = 10.0 * _safe_unit(inp.social_bear_ratio)

    raw = news_component + volume_component + social_component
    return max(0.0, min(100.0, raw))
