"""TDD tests for S_long_straddle_delta_hedged — reference long vol chain-driver.

CANDIDATE-ONLY: this strategy is never promoted to approved/live. It exists
solely to drive the SP-5.1c options execution chain end-to-end.
"""
import numpy as np
import pandas as pd
import pytest

from strategies.implementations.S_long_straddle_delta_hedged import LongStraddleDeltaHedged

UNDERLYING = 'SPY'


def _minimal_args():
    """Build the minimal args that generate_signals needs — matches S_short_straddle_vrp shape."""
    dates = pd.date_range('2024-01-01', periods=42, freq='B')
    prices = pd.DataFrame(
        {UNDERLYING: np.linspace(480.0, 500.0, len(dates))},
        index=dates,
    )
    regime = {'state': 'LOW_VOL'}
    universe = [UNDERLYING]
    aux_data = None
    return prices, regime, universe, aux_data


def test_emits_long_vol_delta_hedged_straddle():
    strat = LongStraddleDeltaHedged(parameters={})
    prices, regime, universe, aux_data = _minimal_args()
    sigs = strat.generate_signals(prices, regime, universe, aux_data)
    assert sigs, 'expected at least one signal'
    s = sigs[0]
    assert s.ticker == 'SPY'
    assert s.direction == 'BUY_VOL'
    assert s.option_spec is not None
    assert s.option_spec.structure == 'straddle'
    assert s.option_spec.hedge == 'delta'


def test_instrument_class_is_option():
    assert LongStraddleDeltaHedged.instrument_class == 'option'
