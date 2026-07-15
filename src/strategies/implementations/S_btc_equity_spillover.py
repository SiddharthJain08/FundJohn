"""
BTC → Equity Spillover — crypto momentum spillover into correlated equities.

Hypothesis: large BTC moves propagate with a lag into the equities whose
returns co-move most with BTC (crypto-adjacent names: miners, exchanges,
fintech, high-beta tech). When BTC's trailing 10-day return exceeds +15%,
LONG the top-decile 120d-BTC-correlated liquid names; below -15%, SHORT
them. Weekly cadence, at most 8 names per firing.

Data: the engine's wide close panel (must carry BTC-USD alongside equities).
Equity math runs on the equity trading calendar (union-calendar weekend rows
dropped); BTC is aligned to those same equity dates by reindex so the
correlation is computed over a common calendar.
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

__all__ = ['BtcEquitySpillover']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_btc_equity_spillover'


class BtcEquitySpillover(BaseStrategy):
    """Big BTC 10d move -> trade the most BTC-correlated liquid equities."""

    id                = STRATEGY_ID
    name              = 'BTC Equity Spillover'
    description       = ('BTC-USD trailing 10d return > +15% -> LONG top-decile 120d-BTC-correlated '
                         'liquid names; < -15% -> SHORT them. Weekly, max 8 per leg.')
    tier              = 3
    signal_frequency  = 'weekly'
    min_lookback      = 140
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 8

    CORR_DAYS       = 120
    MIN_CORR_OBS    = 100
    TRIGGER_DAYS    = 10
    MOVE_THRESHOLD  = 0.15
    LEG_COUNT       = 8
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

    def default_parameters(self) -> dict:
        return {
            'corr_days':      self.CORR_DAYS,
            'trigger_days':   self.TRIGGER_DAYS,
            'move_threshold': self.MOVE_THRESHOLD,
            'leg_count':      self.LEG_COUNT,
            'pool_size':      500,
        }

    @staticmethod
    def _week_boundary(index: pd.DatetimeIndex) -> bool:
        """True on the first equity trading day of a new ISO week."""
        if len(index) < 2:
            return False
        d1 = pd.Timestamp(index[-1]).isocalendar()
        d0 = pd.Timestamp(index[-2]).isocalendar()
        return (d1.year, d1.week) != (d0.year, d0.week)

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
        if 'BTC-USD' not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Equity trading calendar: drop union-calendar rows where every equity
        # is NaN (weekend rows contributed solely by crypto tickers).
        eq_cols = [c for c in prices.columns
                   if not str(c).startswith('^') and '-USD' not in str(c)
                   and '=F' not in str(c) and '=X' not in str(c)]
        if not eq_cols:
            print('[debug] signals=0', file=sys.stderr)
            return []
        eq = prices.loc[prices[eq_cols].notna().any(axis=1).values]
        if len(eq) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []
        # Only act on equity trading days (weekend bars re-see Friday's panel).
        if eq.index[-1] != prices.index[-1]:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not self._week_boundary(eq.index):
            print('[debug] signals=0', file=sys.stderr)
            return []

        trigger_days = int(self.parameters.get('trigger_days', self.TRIGGER_DAYS))
        threshold    = float(self.parameters.get('move_threshold', self.MOVE_THRESHOLD))
        corr_days    = int(self.parameters.get('corr_days', self.CORR_DAYS))

        # BTC aligned to the equity calendar (reindex — same dates as eq rows).
        btc = prices['BTC-USD'].reindex(eq.index).astype('float64')
        if btc.iloc[-(corr_days + 1):].notna().sum() < self.MIN_CORR_OBS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        b_now  = btc.iloc[-1]
        b_then = btc.iloc[-(trigger_days + 1)]
        if not (np.isfinite(b_now) and np.isfinite(b_then)) or b_then <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []
        btc_ret = float(b_now) / float(b_then) - 1.0

        if btc_ret > threshold:
            direction, base = 'LONG', self.BASE_SIZE_LONG
        elif btc_ret < -threshold:
            direction, base = 'SHORT', self.BASE_SIZE_SHORT
        else:
            print('[debug] signals=0', file=sys.stderr)
            return []

        pool = liquid_pool(eq, max_names=int(self.parameters.get('pool_size', 500)),
                           lookback=60)
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in eq.columns]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # 120d correlation of equity daily returns vs BTC daily returns on the
        # SAME equity-calendar dates (pairwise-NaN aware via corrwith).
        window = eq[pool].astype('float64').iloc[-(corr_days + 1):]
        eq_rets  = window.pct_change().iloc[1:]
        btc_rets = btc.iloc[-(corr_days + 1):].pct_change().iloc[1:]
        valid = eq_rets.notna().sum() >= self.MIN_CORR_OBS
        eq_rets = eq_rets.loc[:, valid[valid].index]
        if eq_rets.shape[1] < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []
        corr = eq_rets.corrwith(btc_rets).dropna()
        if len(corr) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Top decile by BTC correlation; spillover only makes sense for
        # positively co-moving names.
        rank_pct = corr.rank(pct=True)
        leg = corr[(rank_pct >= 0.90) & (corr > 0)].sort_values(ascending=False)
        leg = leg.head(int(self.parameters.get('leg_count', self.LEG_COUNT)))
        if leg.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]
        abs_move = abs(btc_ret)
        confidence = 'HIGH' if abs_move > 0.25 else ('MED' if abs_move > 0.20 else 'LOW')

        signals: List[Signal] = []
        for ticker, c in leg.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                continue
            price = float(raw)
            series = eq[ticker].dropna()
            stops = self.compute_stops_and_targets(series, direction, price,
                                                   regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(base * scale, 6),
                confidence=confidence,
                signal_params={
                    'btc_ret_10d':  round(btc_ret, 4),
                    'btc_corr_120d': round(float(c), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
