"""
Synthetic strategy fixture used by scripts/redteam_regression_check.js to
calibrate strategy_redteam.js. Not registered in src/strategies/manifest.json.

Trend-following strategy: a 20-day moving average sets the regime filter,
and a short-horizon close-to-close momentum reading confirms direction.
"""
from __future__ import annotations
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal


class FixtureBeta(BaseStrategy):
    """20-day MA trend filter with a close-to-close momentum confirmation."""

    id                = 'FIXTURE_B'
    name              = 'MA Trend + Momentum Confirmation'
    description       = '20-day moving-average trend filter confirmed by close-to-close momentum.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 25
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            'ma_window': 20,
        }

    def generate_signals(self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        ma_window = self.parameters.get('ma_window', 20)
        signals: List[Signal] = []

        for ticker in universe:
            if ticker not in prices.columns:
                continue
            closes = prices[ticker].dropna()
            if len(closes) < ma_window + 2:
                continue

            ma = closes.rolling(ma_window).mean()

            # Momentum confirmation: compare the current close to the
            # adjacent close.
            confirm_close = closes.shift(1)
            current       = float(closes.iloc[-1])
            confirm_val   = confirm_close.iloc[-1]
            confirm       = float(confirm_val) if pd.notna(confirm_val) else current
            trend_ok      = current > float(ma.iloc[-1])

            if not trend_ok:
                continue

            direction = 'LONG' if current > confirm else 'SHORT'

            stop = current * (0.98 if direction == 'LONG' else 1.02)
            t1   = current * (1.03 if direction == 'LONG' else 0.97)
            t2   = current * (1.06 if direction == 'LONG' else 0.94)
            t3   = current * (1.10 if direction == 'LONG' else 0.90)

            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = current,
                stop_loss         = round(stop, 4),
                target_1          = round(t1, 4),
                target_2          = round(t2, 4),
                target_3          = round(t3, 4),
                position_size_pct = 0.05,
                confidence        = 'MED',
                signal_params     = {'ma_window': ma_window, 'regime': regime_state},
            ))

        return signals[:self.MAX_SIGNALS]
