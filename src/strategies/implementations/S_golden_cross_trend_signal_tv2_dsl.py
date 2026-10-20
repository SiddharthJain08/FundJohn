"""S_golden_cross_trend_signal_tv2_dsl - the expression-DSL twin of
``S_golden_cross_trend_signal_tv2`` (persistent SMA50 > SMA200 trend state,
long-only, LOW_VOL/TRANSITIONING).

Original: ``S_golden_cross_trend_signal_tv2.GoldenCrossTrendStateTV2``. The rule
is one formula, ``(sma(close, 50) - sma(close, 200)) / close > 0``; the base
class turns it into signals (strongest spread first, capped at MAX_SIGNALS).

Divergences from the original (documented, not bugs):
  - A ticker with no bar on the evaluation row emits nothing; the original
    would use its stale last value (``prices[t].dropna()``).
  - Confidence is a flat 'MED' and sizing is SIGNAL_BASE_SIZE, not the
    original's spread-banded confidence / MAX_WEIGHT clamp.
"""
from __future__ import annotations

from strategies.base import BaseStrategy

__all__ = ['GoldenCrossTrendStateTV2Dsl']


class GoldenCrossTrendStateTV2Dsl(BaseStrategy):
    id                = 'S_golden_cross_trend_signal_tv2_dsl'
    name              = 'GoldenCrossTrendStateTV2Dsl'
    description       = ('Golden-cross trend state (SMA50 above SMA200, spread/close > 0), long-only, '
                         'expressed as a SIGNAL formula; DSL twin of S_golden_cross_trend_signal_tv2.')
    tier              = 3
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    instrument_class  = 'equity'
    MAX_SIGNALS       = 50

    SIGNAL           = "(sma(close, 50) - sma(close, 200)) / close"
    SIGNAL_DIRECTION = 'long'
    SIGNAL_THRESHOLD = 0.0
    SIGNAL_BASE_SIZE = 0.05

    def default_parameters(self) -> dict:
        return {'base_size': self.SIGNAL_BASE_SIZE}
