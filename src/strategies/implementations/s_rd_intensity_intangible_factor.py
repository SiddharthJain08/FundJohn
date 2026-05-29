"""
S_rd_intensity_intangible_factor — R&D Intensity / Intangible Factor

Stocks with high R&D expenditure relative to market cap (beaten-down,
poor-past-return firms) earn large excess returns because GAAP expensing of
intangibles causes systematic market undervaluation of future economic benefits
from innovation. (Chan, Lakonishok & Sougiannis 2001; Eberhart, Maxwell &
Siddique 2004.)

STATUS: staging — awaiting financials data backfill
        (provider: fmp; gap: 2022-12-10→2023-06-30).
        Returns [] until data gate clears.
"""
from __future__ import annotations
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['S_rd_intensity_intangible_factor']


class S_rd_intensity_intangible_factor(BaseStrategy):
    id   = 'S_rd_intensity_intangible_factor'
    name = 'RDIntensityIntangibleFactor'
    tier = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        # Awaiting financials data backfill — returns no signals until data gate clears.
        return []
