"""
Rebalancing Premium In Cryptocurrencies — port of QuantConnect reference impl.

Source: https://quantpedia.com/strategies/rebalancing-premium-in-cryptocurrencies/

Original strategy goes long a daily-rebalanced equal-weight portfolio of 26 cryptos
and shorts a drifting buy-and-hold portfolio at 70% weight. The rebalancing premium
arises because high crypto volatility causes the rebalanced portfolio to systematically
sell winners and buy laggards, generating excess return vs buy-and-hold.

Adaptation: short leg omitted (crypto shorting not available on Alpaca). We implement
the long rebalanced leg only: daily equal-weight LONG on BTC-USD and ETH-USD, the two
assets with deep price history in prices.parquet.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['RebalancingPremiumInCryptocurrencies']

INSTRUMENT_CLASS = 'crypto'

# Fixed crypto basket — original had 26 Bitfinex symbols; we use the two with
# full daily history in prices.parquet.
BASKET = ['BTC-USD', 'ETH-USD']
N = len(BASKET)
EQUAL_WEIGHT     = 1.0 / N     # 0.50 per asset
BASE_GROSS       = 0.30        # 30% NAV gross pre-regime-scale, split equally
MOMENTUM_LOOKBACK = 21         # 21-day window for confidence scoring only
TRADING_DAYS     = 365


class RebalancingPremiumInCryptocurrencies(BaseStrategy):
    """Daily equal-weight rebalancing across BTC-USD and ETH-USD captures the
    rebalancing premium by systematically selling winners and buying laggards."""

    id                = 'S_ast_rebalancing_premium_in_cryptocurrencies'
    name              = 'Rebalancing Premium In Cryptocurrencies'
    description       = (
        'Daily equal-weight rebalancing across BTC-USD and ETH-USD captures the '
        'rebalancing premium by systematically selling winners and buying laggards.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = MOMENTUM_LOOKBACK + 1
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 2

    def default_parameters(self) -> dict:
        return {'base_gross': BASE_GROSS, 'mom_lookback': MOMENTUM_LOOKBACK}

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        available = [t for t in BASKET if t in prices.columns]
        if len(available) < 2:
            print(
                f'[RebalancingPremiumInCryptocurrencies] missing tickers, '
                f'available={available}, signals=0',
                file=sys.stderr,
            )
            return []

        mom_lookback = int(self.parameters.get('mom_lookback', MOMENTUM_LOOKBACK))
        base_gross   = float(self.parameters.get('base_gross', BASE_GROSS))
        scale        = self.position_scale(regime_state)
        n            = len(available)
        signals      = []

        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < mom_lookback + 1:
                continue

            current_price = float(series.iloc[-1])
            if current_price <= 0:
                continue

            # Per-asset position size: equal weight, regime-scaled
            pos_size = round((base_gross / n) * scale, 4)
            if pos_size < 0.001:
                continue

            # 21-day momentum used for confidence scoring only; we always rebalance
            # (mean-reversion between assets is the alpha source, not trend)
            mom = float(series.iloc[-1]) / float(series.iloc[-mom_lookback]) - 1.0
            confidence: str
            if mom > 0.10:
                confidence = 'HIGH'
            elif mom > -0.10:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            st = self.compute_stops_and_targets(
                series, direction='LONG', current_price=current_price,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = float(st['stop']),
                target_1          = float(st['t1']),
                target_2          = float(st['t2']),
                target_3          = float(st['t3']),
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'rebalance_weight': round(EQUAL_WEIGHT, 4),
                    'mom_21d':          round(mom, 4),
                    'regime':           regime_state,
                    'n_assets':         n,
                },
            ))

        print(
            f'[RebalancingPremiumInCryptocurrencies] signals={len(signals)}',
            file=sys.stderr,
        )
        return signals
