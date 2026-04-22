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

# Canonical regime vocabulary — the HMM classifier only ever emits these four.
# Strategies that declare any other tag in `active_in_regimes` will never fire
# because `should_run()` does exact-string membership. See docs below.
CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

# Soft synonyms StrategyCoder sometimes emits from recent paper vocabularies.
# At class-definition time we expand synonyms into canonical tags so legacy
# strategies keep working. New strategies should use canonical directly.
REGIME_SYNONYMS = {
    'NEUTRAL':  ('LOW_VOL', 'TRANSITIONING'),   # calm-to-mildly-uncertain band
    'RISK_OFF': ('HIGH_VOL', 'CRISIS'),         # elevated-stress band
    'RISK_ON':  ('LOW_VOL', 'TRANSITIONING'),   # mirror of RISK_OFF
}

# Tighten ATR-based stops in high-vol regimes to preserve R:R geometry.
# Without this, 2× ATR stops balloon to 6-9% in TRANSITIONING/HIGH_VOL while
# targets remain fixed at 5-20%, collapsing R:R to <1x and making EV negative.
REGIME_ATR_SCALE = {
    'LOW_VOL':       1.00,
    'TRANSITIONING': 0.70,
    'HIGH_VOL':      0.55,
    'CRISIS':        0.35,
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
    # Safety cap: generate_signals should not return more than this many signals.
    # Prevents runaway signal counts at large universe sizes without slicing in each strategy.
    MAX_SIGNALS:      int = 50

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.active_in_regimes is None:
            cls.active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
        # Preserve the author's original declaration so validate_strategy can
        # inspect it and reject bad tags at the candidate→paper gate.
        cls._raw_active_in_regimes = list(cls.active_in_regimes)
        # Normalize non-canonical tags at runtime so legacy/imported strategies
        # don't silently become inert. Synonyms expand; unknown tags are
        # dropped with a warning (they'd never match a HMM-emitted state
        # anyway; silently keeping them hid real bugs).
        normalized: list[str] = []
        seen: set[str] = set()
        for tag in cls.active_in_regimes:
            if tag in CANONICAL_REGIMES:
                if tag not in seen:
                    normalized.append(tag); seen.add(tag)
            elif tag in REGIME_SYNONYMS:
                import warnings
                warnings.warn(
                    f"{cls.__name__}: regime tag '{tag}' is a synonym — expanding to {REGIME_SYNONYMS[tag]}. "
                    f"Use canonical tags {CANONICAL_REGIMES} directly to avoid this warning.",
                    stacklevel=3,
                )
                for exp in REGIME_SYNONYMS[tag]:
                    if exp not in seen:
                        normalized.append(exp); seen.add(exp)
            else:
                import warnings
                warnings.warn(
                    f"{cls.__name__}: unknown regime tag '{tag}' dropped — not in {CANONICAL_REGIMES}.",
                    stacklevel=3,
                )
        cls.active_in_regimes = normalized or ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

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
        regime_state:   str   = 'LOW_VOL',
    ) -> dict:
        """Standard stop/target computation. Reusable across strategies."""
        diff = prices_series.diff().abs()
        atr  = float(diff.rolling(14).mean().iloc[-1]) if len(diff) >= 14 else current_price * 0.02

        # Scale ATR multiplier by regime to preserve R:R geometry in high-vol environments.
        # High vol inflates ATR-based stops without expanding fixed-% targets, killing EV.
        effective_atr_mult = atr_multiplier * REGIME_ATR_SCALE.get(regime_state, 1.0)

        if direction == 'LONG':
            stop = current_price - atr * effective_atr_mult
            t1   = current_price * 1.05
            t2   = current_price * 1.10
            t3   = bull_target or current_price * 1.20
        else:
            stop = current_price + atr * effective_atr_mult
            t1   = current_price * 0.95
            t2   = current_price * 0.90
            t3   = bear_target or current_price * 0.80

        return {
            'stop': round(stop, 4),
            't1':   round(t1,   4),
            't2':   round(t2,   4),
            't3':   round(t3,   4),
        }
