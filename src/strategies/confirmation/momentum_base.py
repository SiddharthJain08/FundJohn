"""Shared cross-sectional momentum base signal for corroboration strategies.

Pure / deterministic: no I/O, no clock, no randomness. Same inputs -> same outputs.
Used by S_options_flow_confirmed_momentum and S_sector_flow_confirmed_momentum so the
'base signal' is identical across both (DRY) and the corroboration lift is measured cleanly.
"""
from __future__ import annotations
from typing import Dict, List, Tuple
import pandas as pd


def momentum_scores(prices: pd.DataFrame, universe: List[str],
                    lookback: int = 63, skip: int = 5) -> Dict[str, float]:
    """{ticker: trailing return over [t-lookback-skip, t-skip]} (skip last `skip` days).

    Skips tickers absent from `prices`, with < lookback+skip history, or non-positive start.
    """
    out: Dict[str, float] = {}
    need = lookback + skip
    for t in universe:
        if t not in prices.columns:
            continue
        s = prices[t].dropna()
        if len(s) < need:
            continue
        start = float(s.iloc[-(lookback + skip)])
        end = float(s.iloc[-(skip + 1)])
        if start <= 0:
            continue
        out[t] = end / start - 1.0
    return out


def rank_long_short(scores: Dict[str, float], decile: float = 0.1,
                    max_each: int = 25) -> Tuple[List[str], List[str]]:
    """Top `decile` fraction -> longs, bottom `decile` -> shorts (abs-momentum filtered)."""
    if not scores:
        return [], []
    ser = pd.Series(scores).sort_values(ascending=False)
    n = max(1, int(len(ser) * decile))
    longs = [t for t in ser.head(n).index if scores[t] > 0][:max_each]
    shorts = [t for t in ser.tail(n).index if scores[t] < 0][:max_each]
    return longs, shorts
