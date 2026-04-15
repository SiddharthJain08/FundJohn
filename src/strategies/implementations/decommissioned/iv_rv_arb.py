"""
S15 — IV/RV Arbitrage
Sell vol when IV30 > HV20 * 1.15 (vol premium >= 15%).
Sell straddles / strangles in elevated IV names. Active in LOW_VOL regime only.
Options data via aux_data['options'].
"""

import pandas as pd
import numpy as np
from typing import List
from ..base import BaseStrategy, Signal


class IVRVArb(BaseStrategy):
    id = 'iv_rv_arb'
    name             = 'IV/RV Arbitrage'
    description      = "Sell vol when IV30 > HV20 * 1.15 in liquid names."
    tier             = 2
    signal_frequency = 'daily'
    min_lookback     = 30
    active_in_regimes = ['LOW_VOL']

    def default_parameters(self) -> dict:
        return {
            'min_iv_rv_ratio': 1.15,    # IV must be 15% above HV
            'min_iv_abs':      0.20,    # IV30 must be at least 20% (not too low)
            'max_iv_abs':      0.80,    # IV30 ceiling (tail risk)
            'min_option_vol':  100,     # minimum daily options volume
            'base_size_pct':   0.02,    # 2% base (vol selling is risk-adjusted)
        }

    def _compute_hv20(self, ts: pd.Series) -> float:
        """20-day historical volatility (annualized)."""
        if len(ts) < 21:
            return 0.0
        log_ret = np.log(ts / ts.shift(1)).dropna()
        hv = float(log_ret.iloc[-20:].std() * np.sqrt(252))
        return hv

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        options_data = (aux_data or {}).get('options', {})
        if not options_data:
            return []

        scale   = self.position_scale(regime_state)
        p       = self.parameters
        signals = []

        for ticker in universe:
            if ticker not in prices.columns:
                continue

            opts = options_data.get(ticker, {})
            if not opts:
                continue

            iv30     = opts.get('iv30', 0) or 0
            opt_vol  = opts.get('volume', 0) or 0

            if iv30 < p['min_iv_abs'] or iv30 > p['max_iv_abs']:
                continue
            if opt_vol < p['min_option_vol']:
                continue

            ts = prices[ticker].dropna()
            if len(ts) < self.min_lookback:
                continue

            hv20 = self._compute_hv20(ts)
            if hv20 <= 0:
                continue

            iv_rv_ratio = iv30 / hv20
            if iv_rv_ratio < p['min_iv_rv_ratio']:
                continue

            current_price = float(ts.iloc[-1])
            # For vol selling: stop is defined as IV doubling (double the premium)
            # Targets are IV returning to HV level
            vol_premium = iv30 - hv20

            stops = self.compute_stops_and_targets(
                ts, 'SHORT', current_price, atr_multiplier=3.0
            )

            conf = 'HIGH' if iv_rv_ratio >= 1.30 else 'MED'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SELL_VOL',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(p['base_size_pct'] * scale, 4),
                confidence        = conf,
                signal_params     = {
                    'iv30':         round(iv30, 4),
                    'hv20':         round(hv20, 4),
                    'iv_rv_ratio':  round(iv_rv_ratio, 4),
                    'vol_premium':  round(vol_premium, 4),
                    'option_vol':   int(opt_vol),
                },
            ))

        signals.sort(key=lambda s: s.signal_params.get('iv_rv_ratio', 0), reverse=True)
        return signals[:5]
