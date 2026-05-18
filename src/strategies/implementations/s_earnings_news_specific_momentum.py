"""
S_earnings_news_specific_momentum — Earnings-News Stock-Specific Momentum

Stock prices systematically under-react to firm-level earnings news, producing
a durable stock-specific momentum component with cleaner alpha and lower risk
than broad cross-sectional price momentum (Chan, Jegadeesh & Lakonishok, 1996).

STATUS: staging — awaiting earnings + earnings_calendar backfill (provider: fmp,
        gap: 2024-12-28 → 2025-03-31). Returns [] until data is ready.
"""
from __future__ import annotations
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['S_earnings_news_specific_momentum']


class S_earnings_news_specific_momentum(BaseStrategy):
    id   = 'S_earnings_news_specific_momentum'
    name = 'EarningsNewsSpecificMomentum'
    tier = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        # Awaiting earnings + earnings_calendar backfill — returns no signals until data gate clears.
        return []
