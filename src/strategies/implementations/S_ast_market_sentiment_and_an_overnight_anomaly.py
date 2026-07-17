"""
Market Sentiment and Overnight Anomaly — ETP implementation.

Source: https://quantpedia.com/strategies/market-sentiment-and-an-overnight-anomaly/
Reference: Quantpedia / Brain Market Sentiment (BMS) + CBOE VIX overlay.

Original logic: Buy SPY at close (MOC) and sell at next open (MOO) when up to
three sentiment filters are met: (1) SPY above 20-day SMA, (2) VIX below 20-day
SMA, (3) BMS (Brain Market Sentiment) above 20-day SMA. Each condition adds 1/3
to the notional weight. BMS is a proprietary Quantpedia feed — unavailable in our
data stack; the strategy degrades to the two available conditions (max weight 2/3).

Implementation: evaluate prior-close values daily; emit LONG when weight > 0;
position_size_pct = weight × regime_scale.  The overnight MOO exit is approximated
by letting the sizer re-evaluate every day — positions naturally flip to FLAT
when no conditions are met.
"""
from __future__ import annotations

import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['MarketSentimentOvernightAnomaly']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_ast_market_sentiment_and_an_overnight_anomaly'

SMA_PERIOD  = 20
SPY_TICKER  = 'SPY'
VIX_TICKER  = '^VIX'


class MarketSentimentOvernightAnomaly(BaseStrategy):
    """Overnight SPY hold scaled by number of sentiment filters met (SPY SMA, VIX SMA).

    Source: https://quantpedia.com/strategies/market-sentiment-and-an-overnight-anomaly/
    """

    id                = STRATEGY_ID
    name              = 'Market Sentiment and Overnight Anomaly'
    description       = (
        'Buy SPY overnight when sentiment filters align: SPY above 20d SMA '
        'and VIX below 20d SMA; position scales with number of conditions met.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = SMA_PERIOD + 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'sma_period':   SMA_PERIOD,
            'base_pos_size': 0.50,   # max allocation at full weight
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if SPY_TICKER not in prices.columns:
            print(f'[{STRATEGY_ID}] SPY not in prices', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        sma_period = int(self.parameters.get('sma_period', SMA_PERIOD))
        base_pos   = float(self.parameters.get('base_pos_size', 0.50))

        spy_series = prices[SPY_TICKER].dropna()
        if len(spy_series) < sma_period + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy_sma = spy_series.rolling(sma_period).mean()
        spy_price   = float(spy_series.iloc[-1])
        spy_sma_val = float(spy_sma.iloc[-1])

        if pd.isna(spy_sma_val):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Condition 1: SPY above 20-day SMA
        weight = 0.0
        spy_above_sma = spy_price > spy_sma_val
        if spy_above_sma:
            weight += 1 / 3

        # Condition 2: VIX below 20-day SMA (if available)
        vix_available = False
        vix_below_sma = False
        if VIX_TICKER in prices.columns:
            vix_series = prices[VIX_TICKER].dropna()
            if len(vix_series) >= sma_period:
                vix_sma = vix_series.rolling(sma_period).mean()
                vix_price   = float(vix_series.iloc[-1])
                vix_sma_val = float(vix_sma.iloc[-1])
                if not pd.isna(vix_sma_val):
                    vix_available = True
                    vix_below_sma = vix_price < vix_sma_val
                    if vix_below_sma:
                        weight += 1 / 3

        # Condition 3: BMS not available — skip
        # Max achievable weight: 2/3 (with VIX available) or 1/3 (without VIX)

        if weight <= 0.0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale    = self.position_scale(regime_state)
        pos_size = float(min(base_pos * weight * 3.0 * scale, 1.0))
        # weight is at most 2/3; multiply by 3 so full-weight → base_pos allocation

        if pos_size < 0.01:
            print('[debug] signals=0', file=sys.stderr)
            return []

        stops = self.compute_stops_and_targets(
            spy_series, direction='LONG', current_price=spy_price,
            regime_state=regime_state,
        )

        n_met      = round(weight * 3)
        confidence = 'HIGH' if n_met >= 2 else 'MED'

        sig = Signal(
            ticker            = SPY_TICKER,
            direction         = 'LONG',
            entry_price       = spy_price,
            stop_loss         = float(stops['stop']),
            target_1          = float(stops['t1']),
            target_2          = float(stops['t2']),
            target_3          = float(stops['t3']),
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'weight':         round(weight, 4),
                'conditions_met': n_met,
                'spy_above_sma':  spy_above_sma,
                'vix_available':  vix_available,
                'vix_below_sma':  vix_below_sma,
                'bms_available':  False,
                'regime':         regime_state,
                'scale':          scale,
            },
        )

        print(f'[debug] signals=1', file=sys.stderr)
        return [sig]
