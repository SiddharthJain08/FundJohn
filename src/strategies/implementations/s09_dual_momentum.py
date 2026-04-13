"""
S9 — Dual Momentum
Gary Antonacci's dual momentum: absolute momentum (SPY vs T-bill) + relative momentum.
Active in LOW_VOL and TRANSITIONING regimes.
"""

import pandas as pd
import numpy as np
from typing import List
from ..base import BaseStrategy, Signal


class DualMomentum(BaseStrategy):
    id               = 'S9_dual_momentum'
    name             = 'Dual Momentum'
    description      = "Antonacci dual momentum: absolute (SPY vs cash) + relative cross-asset."
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            'lookback_months': 12,   # 12-1 month
            'skip_months':     1,    # skip most recent month
            'risk_free_annual': 0.05,
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        scale   = self.position_scale(regime_state)
        lb      = self.parameters['lookback_months']
        skip    = self.parameters['skip_months']
        rf_ann  = self.parameters['risk_free_annual']

        # Need SPY in prices
        if 'SPY' not in prices.columns:
            return []

        # Lookback period: (lb + skip) months back, skip most recent `skip` months
        trading_days_lb   = (lb + skip) * 21
        trading_days_skip = skip * 21

        if len(prices) < trading_days_lb + 5:
            return []

        top_n   = self.parameters.get('top_n', 5)
        signals = []
        tickers = [
            t for t in universe
            if t in prices.columns and t != 'SPY'
            and not t.startswith('^')
            and not t.endswith('=F')
            and '-USD' not in t
        ]

        spy_series = prices['SPY'].dropna()
        if len(spy_series) < trading_days_lb:
            return []

        # Absolute momentum: SPY 12-1 month return vs risk-free
        spy_ret = (spy_series.iloc[-1 - trading_days_skip] / spy_series.iloc[-trading_days_lb]) - 1
        rf_period = rf_ann * (lb / 12)
        spy_beats_cash = spy_ret > rf_period

        for ticker in tickers:
            ts = prices[ticker].dropna()
            if len(ts) < trading_days_lb:
                continue

            # Relative momentum: 12-1 month return
            try:
                ret = (ts.iloc[-1 - trading_days_skip] / ts.iloc[-trading_days_lb]) - 1
            except Exception:
                continue

            if not spy_beats_cash:
                # Absolute filter fails — go to cash / no signal
                continue

            if ret <= 0:
                continue

            current_price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', current_price)

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(0.20 / top_n * scale, 4),
                confidence        = 'MED' if ret > 0.10 else 'LOW',
                signal_params     = {
                    'lookback_ret':    round(ret, 4),
                    'spy_ret':         round(spy_ret, 4),
                    'spy_beats_cash':  spy_beats_cash,
                    'rf_period':       round(rf_period, 4),
                },
            ))

        signals.sort(key=lambda s: s.signal_params.get('lookback_ret', 0), reverse=True)
        return signals[:top_n]
