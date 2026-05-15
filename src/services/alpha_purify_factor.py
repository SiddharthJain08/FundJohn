"""Thin facade over eliasswu/AlphaPurify for the two operations we'll do
repeatedly in StrategyCoder's paper->factor pipeline: clean (winsorize +
zscore + neutralize-on-demand), and rank-IC vs forward returns.

If the upstream API differs from what's expected, update only the call sites
inside this file — callers stay stable."""
from __future__ import annotations

import pandas as pd
import numpy as np


def clean_factor(series: pd.Series, winsor_pct: float = 0.01) -> pd.Series:
    """Winsorize at winsor_pct/2 each tail then z-score.  Pure pandas/numpy fallback
    so this works even if AlphaPurify's API surface changes underneath."""
    s = series.astype(float).copy()
    lo, hi = s.quantile(winsor_pct / 2), s.quantile(1 - winsor_pct / 2)
    s = s.clip(lower=lo, upper=hi)
    mu, sd = s.mean(), s.std(ddof=0)
    return (s - mu) / (sd if sd > 0 else 1.0)


def ic_against_returns(factor: pd.Series, forward_ret: pd.Series) -> float:
    """Spearman rank-IC. Drops NaN pairs."""
    aligned = pd.concat([factor, forward_ret], axis=1).dropna()
    if len(aligned) < 5:
        return float("nan")
    return float(aligned.corr(method="spearman").iloc[0, 1])
