"""
S_skewness_dispersion_macro — Cross-sectional skewness dispersion macro signal.

MONTHLY: compute 252-day realized skewness per stock; take cross-sectional std
(dispersion); go LONG when dispersion is abnormally low (mean-reversion imminent),
SHORT when dispersion is abnormally high. Conditional weighting on macro regime.
"""
from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['SkewnessDispersionMacro']


class SkewnessDispersionMacro(BaseStrategy):
    """Cross-sectional skewness dispersion macro timing strategy."""

    id          = 'S_skewness_dispersion_macro'
    name        = 'SkewnessDispersionMacro'
    description = 'LONG/SHORT based on cross-sectional realized-skewness dispersion z-score'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    LOOKBACK    = 252
    MIN_STOCKS  = 30   # require at least this many stocks with enough history
    Z_ENTRY     = 1.0  # |z| threshold to generate signal

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
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # Filter universe to columns present in prices
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < self.MIN_STOCKS:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Only run on month-end bars to avoid look-ahead / churn
        if not self._is_month_end(prices):
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        price_data = prices[tickers]

        # Need at least LOOKBACK rows
        if len(price_data) < self.LOOKBACK:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        window = price_data.iloc[-self.LOOKBACK:]
        returns = window.pct_change().dropna()

        # Compute per-stock skewness over the 252-day window
        skew_series = returns.skew()  # pandas skew per column
        skew_series = skew_series.dropna()

        if len(skew_series) < self.MIN_STOCKS:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Cross-sectional dispersion = std of per-stock skewness
        cs_dispersion = float(skew_series.std())

        # Build a rolling history of cs_dispersion for z-scoring
        # Use a rolling 12-month window of monthly snapshots embedded in the price data
        dispersion_history = self._rolling_cs_dispersion(price_data, tickers)
        if dispersion_history is None or len(dispersion_history) < 6:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        hist_mean = float(dispersion_history.mean())
        hist_std  = float(dispersion_history.std())
        if hist_std < 1e-8:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # signal = -zscore(cs_dispersion): low dispersion → LONG, high → SHORT
        z = (cs_dispersion - hist_mean) / hist_std
        signal_z = -z  # invert: high dispersion → negative signal → SHORT

        if abs(signal_z) < self.Z_ENTRY:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        direction = 'LONG' if signal_z < -self.Z_ENTRY else 'SHORT'
        confidence = 'HIGH' if abs(signal_z) > 2.0 else 'MED'

        # Rank stocks by individual skewness for selection
        if direction == 'LONG':
            # Low skewness stocks — beaten down, likely to mean-revert
            candidates = skew_series.nsmallest(20).index.tolist()
        else:
            # High skewness stocks — extended, likely to revert lower
            candidates = skew_series.nlargest(20).index.tolist()

        signals: List[Signal] = []
        n = min(len(candidates), self.MAX_SIGNALS)
        pos_size = round(scale * 0.03, 4)  # 3% per position, regime-scaled

        for ticker in candidates[:n]:
            if ticker not in prices.columns:
                continue
            col = prices[ticker].dropna()
            if col.empty:
                continue
            current_price = float(col.iloc[-1])
            if current_price <= 0:
                continue

            stops = self.compute_stops_and_targets(
                col, direction, current_price, regime_state=regime_state
            )

            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'cs_dispersion': round(cs_dispersion, 6),
                    'signal_z':      round(signal_z, 4),
                    'stock_skew':    round(float(skew_series[ticker]), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_month_end(self, prices: pd.DataFrame) -> bool:
        """True if the last date in the index is the last trading day of its month."""
        if not isinstance(prices.index, pd.DatetimeIndex):
            return True  # can't tell — allow through
        last = prices.index[-1]
        # Check if next date in the index would be a different month
        # (i.e., last is month-end)
        next_day = last + pd.offsets.BDay(1)
        return next_day.month != last.month

    def _rolling_cs_dispersion(
        self, price_data: pd.DataFrame, tickers: List[str]
    ) -> pd.Series | None:
        """
        Compute monthly cross-sectional skewness dispersion over available history.
        Returns a Series of historical dispersion values for z-scoring.
        """
        if len(price_data) < self.LOOKBACK + 21:
            return None

        returns = price_data.pct_change().dropna()
        # Sample every ~21 trading days (monthly)
        step = 21
        dispersion_vals = []

        end_positions = range(self.LOOKBACK, len(returns), step)
        for end_pos in end_positions:
            window = returns.iloc[end_pos - self.LOOKBACK: end_pos]
            skew_s = window.skew().dropna()
            if len(skew_s) < self.MIN_STOCKS:
                continue
            dispersion_vals.append(float(skew_s.std()))

        if not dispersion_vals:
            return None
        return pd.Series(dispersion_vals)
