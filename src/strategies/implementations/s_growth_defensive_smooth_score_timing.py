"""
S_growth_defensive_smooth_score_timing — Growth/Defensive Smooth-Score Timing

A continuous smooth-score function combining rate relief, SPY drawdown depth,
VIX stress relief, and growth-crowding penalty — mapped via tanh to Growth/
Defensive weights with EWMA smoothing — improves risk-adjusted returns over
static growth or balanced ETF allocations by dynamically timing growth vs.
defensive sector exposure.

STATUS: staging — awaiting macro + vol_indices data backfill
        (provider: yfinance; gap: 2024-04-27→2026-05-23).
        Returns [] until data gate clears.
"""
from __future__ import annotations
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['S_growth_defensive_smooth_score_timing']


class S_growth_defensive_smooth_score_timing(BaseStrategy):
    id   = 'S_growth_defensive_smooth_score_timing'
    name = 'GrowthDefensiveSmoothScoreTiming'
    tier = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        # Awaiting macro + vol_indices data backfill — returns no signals until data gate clears.
        return []
