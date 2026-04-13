"""
Base class for all hardcoded strategies.
Every strategy in the registry inherits from this.

CRITICAL RULES for all strategy implementations:
1. generate_signals() must be pure Python — no API calls, no LLM calls
2. All data comes in as pre-loaded DataFrames
3. No randomness unless seeded for reproducibility
4. Handle missing data gracefully — return empty DataFrame, never raise
5. Must be deterministic: same inputs → same outputs always
"""

import pandas as pd
import numpy as np
from abc import ABC, abstractmethod
from typing import List, Optional
from dataclasses import dataclass, field


@dataclass
class Signal:
    ticker:            str
    direction:         str          # LONG | SHORT | SELL_VOL | BUY_VOL | FLAT
    entry_price:       float
    stop_loss:         float
    target_1:          float
    target_2:          float
    target_3:          float
    position_size_pct: float
    confidence:        str          # HIGH | MED | LOW
    signal_params:     dict = field(default_factory=dict)


REGIME_POSITION_SCALE = {
    'LOW_VOL':       1.00,
    'TRANSITIONING': 0.55,
    'HIGH_VOL':      0.35,
    'CRISIS':        0.15,
}


class BaseStrategy(ABC):
    """All hardcoded strategies inherit from this."""

    # Subclasses must define these
    id:               str = ''
    name:             str = ''
    description:      str = ''
    tier:             int = 3
    signal_frequency: str = 'daily'
    min_lookback:     int = 20
    active_in_regimes: List[str] = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.active_in_regimes is None:
            cls.active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def __init__(self, parameters: dict = None):
        self.parameters = parameters or self.default_parameters()

    def default_parameters(self) -> dict:
        return {}

    def should_run(self, regime_state: str) -> bool:
        """Check if this strategy should generate signals in current regime."""
        return regime_state in (self.active_in_regimes or [])

    def position_scale(self, regime_state: str) -> float:
        """Regime-adjusted position scale."""
        return REGIME_POSITION_SCALE.get(regime_state, 0.35)

    @abstractmethod
    def generate_signals(
        self,
        prices:   pd.DataFrame,   # wide: date × ticker closes
        regime:   dict,           # regime JSON
        universe: List[str],      # tickers to consider
        aux_data: dict = None,    # optional: financials, options, etc.
    ) -> List[Signal]:
        """
        Generate signals for today. Uses only data passed in — no external calls.
        Returns list of Signal objects. Empty list = no signals today.
        """
        raise NotImplementedError

    def compute_stops_and_targets(
        self,
        prices_series:  pd.Series,
        direction:      str,
        current_price:  float,
        bull_target:    float = None,
        bear_target:    float = None,
        atr_multiplier: float = 2.0,
    ) -> dict:
        """Standard stop/target computation. Reusable across strategies."""
        diff = prices_series.diff().abs()
        atr  = float(diff.rolling(14).mean().iloc[-1]) if len(diff) >= 14 else current_price * 0.02

        if direction == 'LONG':
            stop = current_price - atr * atr_multiplier
            t1   = current_price * 1.05
            t2   = current_price * 1.10
            t3   = bull_target or current_price * 1.20
        else:
            stop = current_price + atr * atr_multiplier
            t1   = current_price * 0.95
            t2   = current_price * 0.90
            t3   = bear_target or current_price * 0.80

        return {
            'stop': round(stop, 4),
            't1':   round(t1,   4),
            't2':   round(t2,   4),
            't3':   round(t3,   4),
        }
