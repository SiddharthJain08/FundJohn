from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['LowVolatilityUS']


class LowVolatilityUS(BaseStrategy):
    """Rank SP500 stocks asc by 252d realized vol; select lowest-vol decile; equal-weight."""

    id          = 'low_volatility_us'
    name        = 'LowVolatilityUS'
    description = 'Rank stocks asc by 252d realized vol; select lowest-volatility decile; equal-weight'
    tier        = 2

    # NEUTRAL expands to LOW_VOL + TRANSITIONING via base.py synonym resolution
    active_in_regimes = ['LOW_VOL', 'NEUTRAL', 'TRANSITIONING']

    LOOKBACK    = 253   # 252 returns need 253 price points
    DECILE_FRAC = 0.10
    MIN_TICKERS = 10

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

        scale = self.position_scale(regime_state)

        # Intersect universe with available columns
        available = [t for t in universe if t in prices.columns]
        if len(available) < self.MIN_TICKERS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if len(prices) < self.LOOKBACK:
            print('[debug] signals=0', file=sys.stderr)
            return []

        price_window = prices[available].tail(self.LOOKBACK)

        # Annualised realised volatility from log returns
        log_ret = np.log(price_window / price_window.shift(1)).iloc[1:]
        rv = (log_ret.std() * np.sqrt(252)).dropna()

        if rv.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Select lowest-vol decile, cap at MAX_SIGNALS
        n_select = max(1, int(len(rv) * self.DECILE_FRAC))
        selected = rv.nsmallest(min(n_select, self.MAX_SIGNALS))

        # Latest closes for selected tickers
        latest = prices[selected.index].ffill().iloc[-1]

        # Confidence threshold: top half of decile (lowest RV half) → HIGH
        decile_median = float(selected.median())

        # Equal weight before regime scaling
        base_weight = round(1.0 / len(selected), 6)

        signals: List[Signal] = []
        for ticker in selected.index:
            price = float(latest[ticker]) if ticker in latest.index else float('nan')
            if not price or pd.isna(price) or price <= 0:
                continue

            ticker_rv = float(selected[ticker])
            confidence = 'HIGH' if ticker_rv <= decile_median else 'MED'

            stops = self.compute_stops_and_targets(
                prices_series=prices[ticker].dropna(),
                direction='LONG',
                current_price=price,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=round(price, 4),
                stop_loss=round(stops['stop'], 4),
                target_1=round(stops['t1'], 4),
                target_2=round(stops['t2'], 4),
                target_3=round(stops['t3'], 4),
                position_size_pct=round(base_weight * scale, 6),
                confidence=confidence,
                signal_params={
                    'realized_vol_252d': round(ticker_rv, 6),
                    'universe_rv_pctile': round(
                        float((rv < ticker_rv).sum()) / len(rv), 4
                    ),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
