"""
Beta sleeve — long SPY, always.

Spec: docs/specs/2026-08-29-benchmark-relative-sizing-spec.md §2.4 (D4, D8).

This strategy carries no alpha. It exists so that the market's own
regime-conditioned Sharpe enters the sizer through the normal rails: its
backtest sleeves ARE (up to honest costs and entry-regime tagging) SPY's
regime Sharpes, it is eligible in EVERY regime regardless of the activation
slider (activation_assigner `benchmark_sleeve_always_on`, Amendment 1 D-D1)
and sized on its own regime sleeve; alpha tickers are hurdled against S_m
(execution.benchmark_sizing.apply_benchmark_hurdle), which since Amendment 1
is the forward H=1 SPY Sharpe — no longer derived from this strategy's run.

Signal shape: ONE LONG SPY per bar, hold_days = 21 (= the live default hold
cap, so the exit-hook hold-cap parity guard is satisfied). In the backtest the
daily emissions become overlapping 21-day lots whose equal-weighted daily
return is exactly SPY's daily return; live, the sizer's rebalance step nets the
carried target against the held position, so there is no churn. Stop/targets
are set so they never bind. Exit hook (Amendment 1 D-B1): the lot is flattened
on the first bar whose regime differs from its entry regime and re-opened
next bar tagged with the new regime; live, `write_signals`' continuation mint
keeps the position (no churn), the backtest pays one spread per flip.

benchmark_sleeve = True must be mirrored into strategy_registry.parameters at
registration (the sizer reads the registry, never this class).
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

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
    description       = 'Benchmark sleeve: long SPY every bar, hold 21, exits on regime flip; eligible in all regimes.'
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1
    benchmark_sleeve  = True
    exit_hook         = True

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
            print('[debug] signals=0 (SPY last close not numeric)', file=sys.stderr)
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

    def should_exit(self, position: dict, prices: pd.DataFrame,
                    regime: dict, aux_data: dict = None):
        """Amendment 1 D-B1: flatten at today's close when the regime-of-record
        differs from the lot's entry regime (recorded in signal_params at
        signal time), so the sleeve's per-regime Sharpe measures beta WHILE the
        regime holds — the same quantity S_m measures at H=1. Any missing or
        non-canonical state on either side => hold (the hold cap still
        protects). Pure: no price reads."""
        state = regime.get('state') if isinstance(regime, dict) else None
        entry = ((position or {}).get('signal_params') or {}).get('regime')
        if state not in CANONICAL_REGIMES or entry not in CANONICAL_REGIMES:
            return None
        return 'regime_exit' if state != entry else None
