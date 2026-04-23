"""
S-TR-02: Hurst Exponent Regime Flip
====================================

Academic source
---------------
Vogl, M. (2022). "Hurst Exponent Dynamics of S&P 500 Returns: Implications
for Market Efficiency, Long Memory, Multifractality, and Regime Detection."
Quantitative Finance and Economics, 6(4), 700-723.

Builds on:
* Hurst, H. E. (1951). "Long-term storage capacity of reservoirs."
  Transactions of the American Society of Civil Engineers, 116, 770-799.
* Peters, E. (1991). "Chaos and Order in the Capital Markets."

Edge mechanism
--------------
The Hurst exponent H of price returns characterises memory:
    H = 0.5 → random walk (efficient market)
    H > 0.5 → trending (momentum persists)
    H < 0.5 → mean-reverting (anti-persistence)

When the rolling 60-day H of SPY *flips* from > 0.55 (trend regime) to
< 0.45 (anti-persistence), the market is transitioning to a high-volatility
chop regime — typically associated with topping behavior in bull markets.
Vogl (2022) shows H_60d crossings predict 30-day VIX expansion with ~60%
hit rate over 1990-2020.

Trade construction
------------------
This strategy emits regime events that downstream strategies use to:
1. Reduce trend-following allocations (S-T-001 through S-T-005).
2. Increase short-vol harvesting selectively (S-HV13/14/15).
3. Surface "regime change" Discord alerts.

Cool-down: 20 trading days between fires.

Data dependencies
-----------------
prices_df['SPY'] for at least 200 trading days back.
"""

from __future__ import annotations
import math
from typing import List

import numpy as np

from src.strategies.base import Signal
from src.strategies.cohort_base import CohortBaseStrategy
def hurst_rs(series: np.ndarray, min_lag: int = 5, max_lag: int = 30) -> float:
    """Rescaled-range estimator for the Hurst exponent."""
    series = np.asarray(series, dtype=float)
    if series.size < max_lag + 5:
        return 0.5
    lags = np.arange(min_lag, max_lag + 1)
    rs_vals = []
    for L in lags:
        n = series.size // L
        if n < 2:
            continue
        chunks = series[:n * L].reshape(n, L)
        # Mean-adjusted cumulative deviations
        Y = chunks - chunks.mean(axis=1, keepdims=True)
        Z = np.cumsum(Y, axis=1)
        R = Z.max(axis=1) - Z.min(axis=1)
        S = chunks.std(axis=1, ddof=1)
        valid = S > 0
        if not np.any(valid):
            continue
        rs = (R[valid] / S[valid]).mean()
        if rs > 0:
            rs_vals.append((math.log(L), math.log(rs)))
    if len(rs_vals) < 4:
        return 0.5
    xs = np.array([v[0] for v in rs_vals])
    ys = np.array([v[1] for v in rs_vals])
    slope = float(np.polyfit(xs, ys, 1)[0])
    return float(np.clip(slope, 0.1, 0.9))


class HurstRegimeFlip(CohortBaseStrategy):
    id = 'S_TR02_hurst_regime_flip'
    version = '2.0.0'
    regime_filter = ['HIGH_VOL', 'NEUTRAL', 'LOW_VOL']

    H_TREND_MIN: float = 0.55
    H_REVERT_MAX: float = 0.45
    WINDOW_DAYS: int = 60
    COOLDOWN_DAYS: int = 20

    def _generate_signals_cohort(self, market_data: dict, opts_map: dict) -> List[Signal]:
        regime_meta = (market_data or {}).get('regime', {})
        spy_close = (market_data or {}).get('spy_close_history')
        if spy_close is None or len(spy_close) < self.WINDOW_DAYS + 30:
            return []

        days_since_fire = regime_meta.get('days_since_str02_fire', 999)
        if days_since_fire < self.COOLDOWN_DAYS:
            return []

        spy_close = np.asarray(spy_close, float)
        log_ret = np.diff(np.log(spy_close))

        H_now = hurst_rs(log_ret[-self.WINDOW_DAYS:])
        H_prev = hurst_rs(log_ret[-self.WINDOW_DAYS-15:-15])

        flipped = (H_prev >= self.H_TREND_MIN) and (H_now <= self.H_REVERT_MAX)
        if not flipped:
            return []

        # Emit a small directional hedge signal as VXX BUY_VOL
        vxx = opts_map.get('VXX') or opts_map.get('UVXY') or {}
        price = vxx.get('last_price')
        if not (price and price > 0):
            return []
        return [Signal(
            ticker='VXX',
            direction='BUY_VOL',
            entry_price=price,
            stop_loss=round(price * 0.92, 2),
            target_1=round(price * 1.10, 2),
            target_2=round(price * 1.25, 2),
            target_3=round(price * 1.40, 2),
            position_size_pct=0.005,
            confidence='HIGH',
            signal_params={
                'strategy_id': self.id,
                'H_prev_60d': round(H_prev, 3),
                'H_now_60d': round(H_now, 3),
                'kind': 'regime_event',
                'note': 'hurst_regime_flip',
            },
        )]
