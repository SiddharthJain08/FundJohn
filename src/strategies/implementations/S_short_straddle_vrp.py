"""Short-straddle volatility-risk-premium harvester — reference option strategy
for SP-4 Phase 0. Sells the ATM straddle on a liquid underlying and delta-hedges
daily; harvests the gap between implied (priced with a VRP markup) and realized
vol. instrument_class='option'. NOT a tuned production alpha — it proves the
synthetic options engine flows end-to-end (signal -> options_backtest -> metrics).

Re-signalling: fires at most once per N trading days so the engine's
roll-then-reopen loop (not repeated signals) drives continuous holding.
"""
from __future__ import annotations
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal, OptionSpec

UNDERLYING = 'SPY'
RESIGNAL_GAP = 21  # trading days between fresh signals


class ShortStraddleVRP(BaseStrategy):
    id                = 'S_short_straddle_vrp'
    name              = 'Short Straddle VRP'
    description        = ('Delta-hedged short ATM straddle on SPY harvesting the '
                          'volatility risk premium; reference option strategy.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    instrument_class  = 'option'
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']   # avoid short-vol in HIGH_VOL/CRISIS
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
            ticker=UNDERLYING, direction='SELL_VOL', entry_price=S,
            stop_loss=S * 0.90, target_1=S * 1.10, target_2=0.0, target_3=0.0,
            position_size_pct=0.05, confidence='MED',
            option_spec=OptionSpec(
                underlying=UNDERLYING, structure='straddle', hedge='delta',
                dte_target=int(self.parameters.get('dte_target', 30)),
                roll_dte=int(self.parameters.get('roll_dte', 7))),
        )]
