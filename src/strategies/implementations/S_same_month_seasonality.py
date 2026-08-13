"""
Same-Month Seasonality — Heston-Sadka calendar-month return persistence.

Source: Heston & Sadka (2008, Journal of Financial Economics),
"Seasonality in the cross-section of stock returns."

Hypothesis: a stock's average return in a given calendar month persists —
names that historically outperform in month M keep outperforming in month M
(earnings cycles, fiscal-year flows, index-rebalance and dividend timing).
At each month start, rank the liquid pool by the mean same-calendar-month
return over prior years (>=4 observations required) and LONG the top decile.
No short leg — short-side seasonality is weak in the source paper.

Data: close panel only (engine). Requires ~4+ years of panel history before
firing (seasonal means need >=4 prior same-month observations).
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

__all__ = ['SameMonthSeasonality']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_same_month_seasonality'


class SameMonthSeasonality(BaseStrategy):
    """LONG the top decile of historical same-calendar-month mean return."""

    id                = STRATEGY_ID
    name              = 'Same-Month Seasonality'
    description       = ('Heston-Sadka same-calendar-month seasonality: LONG top decile of mean '
                         'same-month return over prior years (>=4 obs) at month start; no short leg.')
    tier              = 2
    signal_frequency  = 'monthly'
    calendar_edge     = True   # window IS the signal; ports across regime flips (2026-08-13)
    min_lookback      = 1008          # ~4 years of trading days
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 12

    MIN_OBS         = 4
    LEG_COUNT       = 12
    BASE_SIZE_LONG  = 0.015

    def default_parameters(self) -> dict:
        return {
            'min_obs':   self.MIN_OBS,
            'leg_count': self.LEG_COUNT,
            'pool_size': 500,
        }

    @staticmethod
    def _equity_rows(prices: pd.DataFrame) -> pd.DataFrame:
        """Drop union-calendar rows where every equity column is NaN."""
        eq_cols = [c for c in prices.columns
                   if not str(c).startswith('^') and '-USD' not in str(c)
                   and '=F' not in str(c) and '=X' not in str(c)]
        if not eq_cols:
            return prices
        return prices.loc[prices[eq_cols].notna().any(axis=1).values]

    @staticmethod
    def _month_boundary(idx: pd.DatetimeIndex) -> bool:
        """True on the first trading day of a month (monthly cadence gate)."""
        if len(idx) < 2:
            return False
        d1 = pd.Timestamp(idx[-1])
        d0 = pd.Timestamp(idx[-2])
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

        eq = self._equity_rows(prices)
        if len(eq) < self.min_lookback or not self._month_boundary(eq.index):
            print('[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        pool = liquid_pool(prices, max_names=int(p.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in eq.columns]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        asof  = pd.Timestamp(eq.index[-1])
        month = asof.month

        c = eq[pool].astype('float64')
        monthly = c.resample('ME').last()
        mret = monthly.pct_change()
        # Completed months only — exclude the just-started (partial) month row.
        mret = mret[mret.index < asof.replace(day=1)]
        same = mret[mret.index.month == month]
        if same.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        min_obs = int(p.get('min_obs', self.MIN_OBS))
        obs     = same.notna().sum()
        score   = same.mean().where(obs >= min_obs).dropna()
        if len(score) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rank_pct = score.rank(pct=True)
        winners  = rank_pct[rank_pct >= 0.90].sort_values(ascending=False)
        winners  = winners.head(int(p.get('leg_count', self.LEG_COUNT)))

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]

        signals: List[Signal] = []
        for ticker, rp in winners.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                continue
            price = float(raw)
            if rp >= 0.97:
                conf = 'HIGH'
            elif rp >= 0.93:
                conf = 'MED'
            else:
                conf = 'LOW'
            series = eq[ticker].dropna()
            stops = self.compute_stops_and_targets(series, 'LONG', price,
                                                   regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(self.BASE_SIZE_LONG * scale, 6),
                confidence=conf,
                signal_params={
                    'month':             month,
                    'seasonal_mean_ret': round(float(score[ticker]), 4),
                    'n_obs':             int(obs[ticker]),
                    'rank_pct':          round(float(rp), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
