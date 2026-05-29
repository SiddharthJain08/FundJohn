"""Sector / broad-market trend-alignment corroboration.

A LONG stock signal is confirmed only when BOTH its SPDR sector ETF and the broad market
are in an aligned uptrend (price above its trailing SMA). SHORT mirrors. ETF closes come from
the wide `prices` frame (no aux plumbing). Pure / deterministic apart from reading `prices`.
"""
from __future__ import annotations
from typing import Tuple
import pandas as pd

SMA_WINDOW = 50
MARKET_ALIGN_MIN = 1


def _trend(prices: pd.DataFrame, sym: str, as_of, window: int = SMA_WINDOW):
    """+1 if last close > SMA(window), -1 if below, 0 if missing/insufficient."""
    if sym not in prices.columns:
        return 0
    s = prices.loc[:as_of, sym].dropna()
    if len(s) < window + 1:
        return 0
    last = float(s.iloc[-1])
    sma = float(s.iloc[-window:].mean())
    if last > sma:
        return 1
    if last < sma:
        return -1
    return 0


def confirm(direction: str, ticker: str, prices: pd.DataFrame, sector_map,
            as_of, window: int = SMA_WINDOW,
            market_align_min: int = MARKET_ALIGN_MIN) -> Tuple[bool, float]:
    """Return (passes, score). False when the ticker is unmapped or ETFs are missing."""
    etf = sector_map.etf_for_ticker(ticker)
    if etf is None:
        return False, 0.0
    want = 1 if direction == 'LONG' else -1 if direction == 'SHORT' else 0
    if want == 0:
        return False, 0.0

    sector_t = _trend(prices, etf, as_of, window)
    market_aligned = sum(1 for m in sector_map.BROAD_MARKET_ETFS
                         if _trend(prices, m, as_of, window) == want)

    passes = (sector_t == want) and (market_aligned >= market_align_min)
    score = want * (0.5 * sector_t + 0.5 * (market_aligned / len(sector_map.BROAD_MARKET_ETFS)))
    return passes, round(float(score), 4)
