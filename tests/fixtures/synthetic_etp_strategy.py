"""Synthetic ETP fixture strategy — FOR TESTS ONLY.

This class must NEVER be added to src/strategies/manifest.json or
src/strategies/registry._IMPL_MAP. It exists solely to exercise the
production BaseStrategy contract in end-to-end smoke tests without
depending on real prices.parquet or live data.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from typing import List
from strategies.base import BaseStrategy, Signal


class SyntheticEtpStrategy(BaseStrategy):
    id   = 'synthetic_etp'
    name = 'SyntheticEtp'
    tier = 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS = 1

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        ticker = 'GLD'
        price = float(prices[ticker].iloc[-1])
        return [Signal(
            ticker=ticker,
            direction='LONG',
            entry_price=price,
            stop_loss=round(price * 0.97, 4),
            target_1=round(price * 1.05, 4),
            target_2=round(price * 1.10, 4),
            target_3=round(price * 1.15, 4),
            position_size_pct=0.02,
            confidence='MED',
        )]
