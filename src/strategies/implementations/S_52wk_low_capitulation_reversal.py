"""
52-Week-Low Capitulation Reversal — high-volume washout bounce at the yearly low.

Hypothesis: a stock pinned within 5% of its 252-day low that prints its first
up-close after >=3 consecutive down days ON a volume spike (60d z-score > 2)
marks seller exhaustion — the marginal capitulation seller is done and the
first up-close is the inflection. LONG the reversal; brackets + 21d max-hold
handle the exit. Per-name re-fire is suppressed for 21 sessions by
recomputing the trigger history from the same panels (stateless cooldown).

Data: close panel (engine) + self-loaded VOLUME panel from prices.parquet via
_extra_panels (point-in-time sliced, 2021+ coverage window).
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import load_wide, liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import load_wide, liquid_pool

__all__ = ['FiftyTwoWeekLowCapitulationReversal']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_52wk_low_capitulation_reversal'


class FiftyTwoWeekLowCapitulationReversal(BaseStrategy):
    """LONG the first high-volume up-close near the 252d low after a down streak."""

    id                = STRATEGY_ID
    name              = '52-Week-Low Capitulation Reversal'
    description       = ('Close within 5% of the 252d low + volume z>2 + first up-close after >=3 '
                         'down days -> LONG capitulation reversal; <=10/day, 21d per-name cooldown.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 280
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 10

    NEAR_LOW_PCT    = 0.05
    VOL_Z_MIN       = 2.0
    DOWN_STREAK     = 3
    COOLDOWN_DAYS   = 21
    BASE_SIZE_LONG  = 0.015

    def default_parameters(self) -> dict:
        return {
            'near_low_pct':  self.NEAR_LOW_PCT,
            'vol_z_min':     self.VOL_Z_MIN,
            'down_streak':   self.DOWN_STREAK,
            'cooldown_days': self.COOLDOWN_DAYS,
            'pool_size':     500,
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

        # Bound per-bar work: only ~283 trailing equity rows are consumed below,
        # so pre-slice a raw tail before the full-panel mask/copy. Union
        # calendar adds <= 2 weekend rows per 5 weekdays, so 620 raw rows always
        # contain >= min_lookback equity rows whenever the full panel would.
        if len(prices) > 620:
            prices = prices.iloc[-620:]
        eq = self._equity_rows(prices)
        if len(eq) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        cooldown = int(p.get('cooldown_days', self.COOLDOWN_DAYS))
        streak   = int(p.get('down_streak', self.DOWN_STREAK))

        pool = liquid_pool(prices, max_names=int(p.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in eq.columns]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Stable superset key -> load_wide cache hits after the first call
        # (a bar-varying `pool` key forced a parquet re-read every bar).
        vols_all = load_wide('volume', list(prices.columns))
        vcols = [t for t in pool if t in vols_all.columns]
        if vols_all.empty or not vcols:
            print('[debug] signals=0', file=sys.stderr)
            return []

        asof   = eq.index[-1]
        closes = eq[pool].iloc[-(252 + cooldown + 10):].astype('float64')
        v      = vols_all[vcols].loc[:asof].reindex(closes.index).astype('float64')

        low252   = closes.rolling(252, min_periods=200).min()
        near_low = closes <= low252 * (1.0 + float(p.get('near_low_pct', self.NEAR_LOW_PCT)))

        vmean = v.rolling(60, min_periods=40).mean()
        vstd  = v.rolling(60, min_periods=40).std()
        with np.errstate(divide='ignore', invalid='ignore'):
            volz = (v - vmean) / vstd

        chg  = closes.diff()
        up   = chg > 0
        down = chg < 0
        down_prior = down.shift(1)
        for k in range(2, streak + 1):
            down_prior = down_prior & down.shift(k)

        trigger = near_low & (volz > float(p.get('vol_z_min', self.VOL_Z_MIN))) & up & down_prior
        trigger = trigger.fillna(False)

        today  = trigger.iloc[-1]
        recent = trigger.iloc[-(cooldown + 1):-1].any()

        cand = [t for t in pool if bool(today.get(t, False)) and not bool(recent.get(t, False))]
        if not cand:
            print('[debug] signals=0', file=sys.stderr)
            return []

        z_today = volz.iloc[-1]
        cand.sort(key=lambda t: float(z_today.get(t, 0.0)), reverse=True)
        cand = cand[:self.MAX_SIGNALS]

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]
        lows    = low252.iloc[-1]

        signals: List[Signal] = []
        for ticker in cand:
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                continue
            price = float(raw)
            z = float(z_today.get(ticker, 0.0))
            if z >= 4.0:
                conf = 'HIGH'
            elif z >= 3.0:
                conf = 'MED'
            else:
                conf = 'LOW'
            series = eq[ticker].dropna()
            stops = self.compute_stops_and_targets(series, 'LONG', price,
                                                   regime_state=regime_state)
            low_ref = float(lows.get(ticker)) if np.isfinite(lows.get(ticker, np.nan)) else None
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
                    'vol_z_60d':      round(z, 4),
                    'pct_above_252d_low': round(price / low_ref - 1.0, 4) if low_ref else None,
                    'down_streak_req': streak,
                },
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
