"""
Beta sleeve — long SPY, always.

Spec: docs/specs/2026-08-29-benchmark-relative-sizing-spec.md §2.4 (D4, D8).

This strategy carries no alpha. It exists so that the market's own
regime-conditioned Sharpe enters the sizer through the normal rails: its
backtest sleeves ARE (up to honest costs and entry-regime tagging) SPY's
regime Sharpes, the activation slider makes it dormant in regimes where SPY
does not clear the slider (TRANSITIONING / HIGH_VOL / CRISIS at the 1.0 slider
as of 2026-08-29), and in LOW_VOL its conviction becomes the base that alpha
tickers must beat (execution.benchmark_sizing.apply_benchmark_hurdle).

Signal shape: ONE LONG SPY per bar, hold_days = 21 (= the live default hold
cap, so the exit-hook hold-cap parity guard is satisfied). In the backtest the
daily emissions become overlapping 21-day lots whose equal-weighted daily
return is exactly SPY's daily return; live, the sizer's rebalance step nets the
carried target against the held position, so there is no churn. Stop/targets
are set so they never bind — the sleeve carries no bracket edge to protect.

benchmark_sleeve = True must be mirrored into strategy_registry.parameters at
registration (the sizer reads the registry, never this class).
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['BetaSpy']

STRATEGY_ID      = 'S_beta_spy'
INSTRUMENT_CLASS = 'etp'
BENCHMARK        = 'SPY'
HOLD_DAYS        = 21
STOP_FRAC        = 0.60    # stop 40% below entry: never binds inside a 21-day lot
TARGET_FRACS     = (5.0, 6.0, 7.0)


class BetaSpy(BaseStrategy):
    id                = STRATEGY_ID
    name              = 'Beta sleeve — long SPY'
    description       = 'Benchmark sleeve: long SPY every bar, hold 21; sized on the market regime Sharpe.'
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1
    benchmark_sleeve  = True

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or BENCHMARK not in prices.columns:
            print('[debug] signals=0 (no SPY column)', file=sys.stderr)
            return []
        px = prices[BENCHMARK].iloc[-1]
        try:
            px = float(px)
        except (TypeError, ValueError):
            return []
        if not (px == px and px > 0):
            print('[debug] signals=0 (SPY last close missing)', file=sys.stderr)
            return []
        state = regime.get('state') if isinstance(regime, dict) else None
        return [Signal(
            ticker            = BENCHMARK,
            direction         = 'LONG',
            entry_price       = px,
            stop_loss         = round(px * STOP_FRAC, 4),
            target_1          = round(px * TARGET_FRACS[0], 4),
            target_2          = round(px * TARGET_FRACS[1], 4),
            target_3          = round(px * TARGET_FRACS[2], 4),
            position_size_pct = 0.10,
            confidence        = 'HIGH',
            signal_params     = {'hold_days': HOLD_DAYS, 'benchmark_sleeve': True, 'regime': state},
        )]
