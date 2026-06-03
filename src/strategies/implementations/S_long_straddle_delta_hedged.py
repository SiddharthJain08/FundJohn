"""Long delta-hedged straddle — reference option strategy for SP-5.1c.

Buys the ATM straddle on SPY and delta-hedges daily; long vol / BUY_VOL
direction. This is a CANDIDATE-ONLY chain-driver: it proves the
signal → options-backtest → single-leg-exec pipeline flows end-to-end.
It is NOT a tuned production alpha and must NEVER be promoted to
approved/live in the registry or manifest.

Re-signalling: fires at most once per N trading days so the engine's
roll-then-reopen loop (not repeated signals) drives continuous holding.
"""
from __future__ import annotations
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal, OptionSpec

UNDERLYING = 'SPY'
RESIGNAL_GAP = 21  # trading days between fresh signals


class LongStraddleDeltaHedged(BaseStrategy):
    id                = 'S_long_straddle_delta_hedged'
    name              = 'Long Straddle Delta Hedged'
    description       = ('Delta-hedged long ATM straddle on SPY; long vol / BUY_VOL. '
                         'Reference option strategy for SP-5.1c chain-driver — '
                         'CANDIDATE-ONLY, never promoted.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    instrument_class  = 'option'
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {'resignal_gap': RESIGNAL_GAP, 'dte_target': 30, 'roll_dte': 7}

    def generate_signals(self, prices: pd.DataFrame, regime: dict,
                         universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty or UNDERLYING not in prices.columns:
            return []
        if not self.should_run(regime.get('state', 'LOW_VOL')):
            return []
        series = prices[UNDERLYING].dropna()
        if len(series) < self.min_lookback:
            return []
        gap = int(self.parameters.get('resignal_gap', RESIGNAL_GAP))
        if (len(series) % gap) != 0:
            return []
        S = float(series.iloc[-1])
        return [Signal(
            ticker=UNDERLYING, direction='BUY_VOL', entry_price=S,
            stop_loss=0.0, target_1=0.0, target_2=0.0, target_3=0.0,
            position_size_pct=0.05, confidence='MED',
            signal_params={'ref_candidate': True},
            option_spec=OptionSpec(
                underlying=UNDERLYING, structure='straddle', hedge='delta',
                strike_rule='atm',
                dte_target=int(self.parameters.get('dte_target', 30)),
                roll_dte=int(self.parameters.get('roll_dte', 7))),
        )]
