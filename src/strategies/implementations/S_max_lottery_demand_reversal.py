"""
MAX5 Lottery-Demand Reversal — cross-sectional lottery-stock anomaly.

Source: Bali, Cakici & Whitelaw (2011, Journal of Financial Economics),
"Maxing out: Stocks as lotteries and the cross-section of expected returns."

Hypothesis: investors overpay for lottery-like payoffs. Stocks with extreme
recent single-day gains (high MAX) attract lottery demand that pushes prices
above fair value and predicts LOW subsequent returns; low-MAX names are
comparatively neglected and outperform. MAX5 = mean of the 5 largest daily
simple returns over the trailing 21 trading days. SHORT the top decile of
MAX5, LONG the bottom decile, rebalanced monthly.

Data: close panel only (no extra parquet fields).
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import liquid_pool

__all__ = ['MaxLotteryDemandReversal']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_max_lottery_demand_reversal'


class MaxLotteryDemandReversal(BaseStrategy):
    """SHORT high-MAX5 lottery names, LONG low-MAX5 neglected names."""

    id                = STRATEGY_ID
    name              = 'MAX5 Lottery-Demand Reversal'
    description       = ('Cross-sectional lottery anomaly (Bali-Cakici-Whitelaw 2011): MAX5 = mean of the '
                         '5 largest daily returns in trailing 21d; SHORT top decile, LONG bottom decile, monthly.')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 90
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 24

    FORMATION_DAYS  = 21
    MAX_K           = 5     # number of largest daily returns averaged
    MIN_VALID_DAYS  = 15
    LEG_COUNT       = 12
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

    def default_parameters(self) -> dict:
        return {
            'formation_days': self.FORMATION_DAYS,
            'max_k':          self.MAX_K,
            'leg_count':      self.LEG_COUNT,
            'pool_size':      500,
        }

    def _month_boundary(self, prices: pd.DataFrame) -> bool:
        """True on the first trading day of a month (monthly cadence gate)."""
        if len(prices) < 2:
            return False
        d1 = pd.Timestamp(prices.index[-1])
        d0 = pd.Timestamp(prices.index[-2])
        return d1.month != d0.month

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty or len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not (self._month_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        fdays = int(self.parameters.get('formation_days', self.FORMATION_DAYS))
        max_k = int(self.parameters.get('max_k', self.MAX_K))
        pool = liquid_pool(prices, max_names=int(self.parameters.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in prices.columns]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Union-calendar safety: drop rows where every pool equity is NaN
        # (weekend/holiday rows contributed by crypto tickers) BEFORE taking
        # the formation tail, so returns are computed close-to-close on real
        # equity sessions only (never across artificial NaN gaps).
        eq = prices[pool].dropna(how='all')
        if len(eq) < fdays + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []
        closes = eq.iloc[-(fdays + 1):].astype('float64')
        rets = closes.pct_change().iloc[1:]
        if rets.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        valid = rets.notna().sum() >= self.MIN_VALID_DAYS
        arr = np.where(np.isnan(rets.values), -np.inf, rets.values)
        top_k = np.sort(arr, axis=0)[-max_k:]
        max5 = pd.Series(top_k.mean(axis=0), index=rets.columns)
        score = max5.where(valid & np.isfinite(max5)).dropna()
        if len(score) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rank_pct = score.rank(pct=True)
        n_leg = min(int(self.parameters.get('leg_count', self.LEG_COUNT)),
                    max(1, int(len(score) * 0.10)))
        # Lottery names (high MAX5) are overpriced -> SHORT; low MAX5 -> LONG.
        shorts = rank_pct[rank_pct >= 0.90].sort_values(ascending=False).head(n_leg)
        longs  = rank_pct[rank_pct <= 0.10].sort_values(ascending=True).head(n_leg)

        scale = self.position_scale(regime_state)
        current = closes.iloc[-1]   # last real equity session (union-calendar safe)

        def _conf(ext: float) -> str:
            if ext >= 0.97:
                return 'HIGH'
            if ext >= 0.93:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for leg, direction, base in ((shorts, 'SHORT', self.BASE_SIZE_SHORT),
                                     (longs, 'LONG', self.BASE_SIZE_LONG)):
            for ticker, rp in leg.items():
                if len(signals) >= self.MAX_SIGNALS:
                    break
                raw = current.get(ticker)
                if raw is None or not np.isfinite(raw) or raw <= 0:
                    continue
                price = float(raw)
                series = prices[ticker].dropna()
                stops = self.compute_stops_and_targets(series, direction, price,
                                                       regime_state=regime_state)
                extremity = float(rp) if direction == 'SHORT' else 1.0 - float(rp)
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=price,
                    stop_loss=stops['stop'],
                    target_1=stops['t1'],
                    target_2=stops['t2'],
                    target_3=stops['t3'],
                    position_size_pct=round(base * scale, 6),
                    confidence=_conf(extremity),
                    signal_params={
                        'max5_21d': round(float(score[ticker]), 5),
                        'rank_pct': round(float(rp), 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
