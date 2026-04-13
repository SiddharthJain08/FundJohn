"""
S10 — Quality Value
High-quality companies trading at discounts: high ROE/ROIC + low EV/EBITDA + low P/FCF.
Fundamentals via aux_data['financials']. Active in LOW_VOL only.
"""

import pandas as pd
import numpy as np
from typing import List
from ..base import BaseStrategy, Signal


class QualityValue(BaseStrategy):
    id               = 'S10_quality_value'
    name             = 'Quality Value'
    description      = "Buy high-ROE/ROIC names at low EV/EBITDA multiples."
    tier             = 2
    signal_frequency = 'weekly'
    min_lookback     = 60
    active_in_regimes = ['LOW_VOL']

    def default_parameters(self) -> dict:
        return {
            'min_roe':          0.15,    # 15% ROE minimum
            'min_roic':         0.10,    # 10% ROIC minimum
            'max_ev_ebitda':    15.0,    # EV/EBITDA ceiling
            'max_p_fcf':        20.0,    # P/FCF ceiling
            'min_gross_margin': 0.30,    # 30% gross margin floor
            'max_debt_equity':  2.0,     # D/E ceiling
            'base_size_pct':    0.04,    # 4% base position size
        }

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        financials = (aux_data or {}).get('financials', {})
        if not financials:
            return []

        scale   = self.position_scale(regime_state)
        p       = self.parameters
        signals = []

        for ticker in universe:
            if ticker not in prices.columns:
                continue
            fin = financials.get(ticker, {})
            if not fin:
                continue

            # Quality filters
            roe          = fin.get('returnOnEquity', 0) or 0
            roic         = fin.get('returnOnInvestedCapital', 0) or 0
            gross_margin = fin.get('grossProfitMargin', 0) or 0
            debt_equity  = fin.get('debtEquityRatio', 999) or 999

            if roe < p['min_roe']:
                continue
            if roic < p['min_roic']:
                continue
            if gross_margin < p['min_gross_margin']:
                continue
            if debt_equity > p['max_debt_equity']:
                continue

            # Value filters
            ev_ebitda = fin.get('enterpriseValueMultiple', 999) or 999
            p_fcf     = fin.get('priceToFreeCashFlowsRatio', 999) or 999

            if ev_ebitda > p['max_ev_ebitda']:
                continue
            if p_fcf > p['max_p_fcf']:
                continue

            ts = prices[ticker].dropna()
            if len(ts) < self.min_lookback:
                continue

            current_price = float(ts.iloc[-1])
            stops = self.compute_stops_and_targets(ts, 'LONG', current_price)

            # Composite quality score for confidence
            q_score = (min(roe / 0.30, 1.0) + min(roic / 0.20, 1.0)) / 2
            conf    = 'HIGH' if q_score > 0.75 else ('MED' if q_score > 0.50 else 'LOW')

            size = round(p['base_size_pct'] * scale, 4)

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = size,
                confidence        = conf,
                signal_params     = {
                    'roe':          round(roe, 4),
                    'roic':         round(roic, 4),
                    'ev_ebitda':    round(ev_ebitda, 2),
                    'p_fcf':        round(p_fcf, 2),
                    'gross_margin': round(gross_margin, 4),
                    'q_score':      round(q_score, 4),
                },
            ))

        # Rank by quality score descending, cap at 10 positions
        signals.sort(key=lambda s: s.signal_params.get('q_score', 0), reverse=True)
        return signals[:10]
