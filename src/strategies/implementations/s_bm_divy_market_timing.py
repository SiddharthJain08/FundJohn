"""
S_bm_divy_market_timing — Book-to-Market / Dividend Yield Market Timing

Aggregate book-to-market ratio and dividend yield of the equal-weighted market
index predict time-series variation in real expected market returns, with high
B/M or high D/P signalling elevated risk premia that the market will subsequently
compensate. (Kothari & Shanken 1997; Pontiff & Schall 1998.)

STATUS: staging — awaiting financials + macro data backfill
        (providers: fmp, yfinance; gap: 2022-12-10→2023-06-30).
        Returns [] until data gate clears.
"""
from __future__ import annotations
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['S_bm_divy_market_timing']


class S_bm_divy_market_timing(BaseStrategy):
    id   = 'S_bm_divy_market_timing'
    name = 'BMDivyMarketTiming'
    tier = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        # Awaiting financials + macro data backfill — returns no signals until data gate clears.
        return []
