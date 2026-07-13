"""
Factor Momentum Rotation — trade the factors that are themselves trending.

Source: Gupta & Kelly (2021), "Factor Momentum Everywhere" (JPM).

Three internal price-only factors are built inside the liquid pool:
  - 12-1 momentum      (return t-252 -> t-21)
  - low-vol            (inverse trailing 63d return vol)
  - 5d short-term reversal (negative trailing 5d return)

For each factor, a trailing DAILY top-minus-bottom-decile spread series is
constructed (day-t spread = mean return of the top decile minus the bottom
decile, deciles formed on the factor value as of t-1 — no same-day
look-ahead). A factor is "on" when its trailing 63d cumulative spread
return is positive. Monthly, allocate LONG the CURRENT top-decile names of
each on-factor (round-robin across on-factors), max 15 total.
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

__all__ = ['FactorMomentumRotation']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_factor_momentum_rotation'


class FactorMomentumRotation(BaseStrategy):
    """Monthly: LONG top-decile names of price factors with positive 63d factor momentum."""

    id                = STRATEGY_ID
    name              = 'Factor Momentum Rotation'
    description       = ('Internal price factors (12-1 momentum, low-vol, 5d reversal) as daily '
                         'top-minus-bottom-decile spread series; monthly LONG the current top-decile '
                         'names of each factor whose trailing 63d spread return > 0. Max 15.')
    tier              = 3
    signal_frequency  = 'monthly'
    min_lookback      = 350
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 15

    SPREAD_DAYS     = 63      # trailing factor-spread window
    MOM_LONG        = 252
    MOM_SKIP        = 21
    LOWVOL_DAYS     = 63
    REV_DAYS        = 5
    TOTAL_PICKS     = 15
    BASE_SIZE_LONG  = 0.015

    def default_parameters(self) -> dict:
        return {
            'spread_days': self.SPREAD_DAYS,
            'total_picks': self.TOTAL_PICKS,
            'pool_size':   500,
        }

    @staticmethod
    def _month_boundary(index: pd.DatetimeIndex) -> bool:
        """True on the first equity trading day of a month."""
        if len(index) < 2:
            return False
        d1 = pd.Timestamp(index[-1])
        d0 = pd.Timestamp(index[-2])
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

        # Equity trading calendar: drop union-calendar all-NaN-equity rows.
        eq_cols = [c for c in prices.columns
                   if not str(c).startswith('^') and '-USD' not in str(c)
                   and '=F' not in str(c) and '=X' not in str(c)]
        if not eq_cols:
            print('[debug] signals=0', file=sys.stderr)
            return []
        eq = prices.loc[prices[eq_cols].notna().any(axis=1).values]
        if len(eq) < self.min_lookback or eq.index[-1] != prices.index[-1]:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not self._month_boundary(eq.index):
            print('[debug] signals=0', file=sys.stderr)
            return []

        pool = liquid_pool(eq, max_names=int(self.parameters.get('pool_size', 500)),
                           lookback=60)
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in eq.columns]
        if len(pool) < 50:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spread_days = int(self.parameters.get('spread_days', self.SPREAD_DAYS))
        total_picks = int(self.parameters.get('total_picks', self.TOTAL_PICKS))

        # Bound the working set: factor history needs MOM_LONG+MOM_SKIP bars
        # before the earliest spread day.
        need = self.MOM_LONG + self.MOM_SKIP + spread_days + 5
        px = eq[pool].astype('float64').iloc[-need:]
        rets = px.pct_change()

        factors = {
            'momentum_12_1': px.shift(self.MOM_SKIP) / px.shift(self.MOM_LONG) - 1.0,
            'low_vol':       -rets.rolling(self.LOWVOL_DAYS).std(),
            'st_reversal':   -(px / px.shift(self.REV_DAYS) - 1.0),
        }

        on_factors: dict = {}       # name -> (spread_63d, current top-decile Series)
        for fname, F in factors.items():
            F = F.replace([np.inf, -np.inf], np.nan)
            # Daily decile spread over the trailing window, deciles on F[t-1].
            rank = F.shift(1).rank(axis=1, pct=True)
            top_ret = rets.where(rank >= 0.90).mean(axis=1)
            bot_ret = rets.where(rank <= 0.10).mean(axis=1)
            spread = (top_ret - bot_ret).iloc[-spread_days:]
            if spread.notna().sum() < int(spread_days * 0.7):
                continue
            spread_ret = float(spread.sum())
            if spread_ret <= 0:
                continue
            f_now = F.iloc[-1].dropna()
            if len(f_now) < 50:
                continue
            r_now = f_now.rank(pct=True)
            top_names = r_now[r_now >= 0.90].sort_values(ascending=False)
            on_factors[fname] = (spread_ret, top_names)

        if not on_factors:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Round-robin across on-factors (strongest factor first) so the book
        # blends every trending style; dedupe; cap 15.
        ordered = sorted(on_factors.items(), key=lambda kv: -kv[1][0])
        queues = {f: list(top.items()) for f, (_, top) in ordered}
        chosen: dict = {}           # ticker -> (factor, rank_pct)
        i = 0
        while len(chosen) < total_picks and any(queues.values()):
            fname = ordered[i % len(ordered)][0]
            q = queues[fname]
            while q:
                ticker, rp = q.pop(0)
                if ticker not in chosen:
                    chosen[ticker] = (fname, float(rp))
                    break
            i += 1
            if i > 10 * total_picks:   # safety: all queues exhausted of new names
                break

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]

        signals: List[Signal] = []
        for ticker, (fname, rp) in chosen.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                continue
            price = float(raw)
            confidence = 'HIGH' if rp >= 0.97 else ('MED' if rp >= 0.93 else 'LOW')
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
                confidence=confidence,
                signal_params={
                    'factor':            fname,
                    'factor_rank_pct':   round(rp, 4),
                    'factor_spread_63d': round(on_factors[fname][0], 4),
                    'on_factors':        sorted(on_factors.keys()),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
