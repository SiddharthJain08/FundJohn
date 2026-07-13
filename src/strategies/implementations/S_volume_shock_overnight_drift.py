"""
Volume Shock Overnight Drift — post-shock continuation on confirmed up-days.

Source: https://www.quantitativo.com/p/volume-shocks-and-overnight-returns
(consumes the pending research_candidates row for this idea).

Hypothesis: an extreme volume shock (volume z-score > 2.5 vs the trailing
60d distribution) on a strong up-day (day return > +2%) marks the arrival of
new information plus institutional participation whose demand is not fully
absorbed intraday; the excess return drifts into the following session(s).
LONG the shocked names next day (backtest fills t+1 close). Daily scan with
a 5-session per-name cooldown (weekly dedupe) recomputed from the price
index itself — a name whose trigger already fired within the trailing week
is skipped — so a multi-day shock episode is traded once. Capped at 15
signals per firing.

Data: close panel (engine) + self-loaded VOLUME panel from prices.parquet
via _extra_panels (point-in-time sliced, 2021+ coverage window).
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

__all__ = ['VolumeShockOvernightDrift']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_volume_shock_overnight_drift'


class VolumeShockOvernightDrift(BaseStrategy):
    """LONG extreme-volume up-day shocks for next-day drift continuation."""

    id                = STRATEGY_ID
    name              = 'Volume Shock Overnight Drift'
    description       = ('Volume z-score > 2.5 vs 60d on an up-day with day return > +2% -> LONG next-day '
                         'drift continuation; daily scan, 5-session per-name cooldown, <=15 signals/day.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 70          # 60d volume z-score + cooldown recompute + buffer
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 15

    Z_WINDOW           = 60
    MIN_VALID_DAYS     = 40
    VOL_Z_MIN          = 2.5
    DAY_RET_MIN        = 0.02
    COOLDOWN_SESSIONS  = 5          # weekly per-name dedupe, in trading sessions
    BASE_SIZE_LONG     = 0.015

    def default_parameters(self) -> dict:
        return {
            'z_window':          self.Z_WINDOW,
            'vol_z_min':         self.VOL_Z_MIN,
            'day_ret_min':       self.DAY_RET_MIN,
            'cooldown_sessions': self.COOLDOWN_SESSIONS,
            'pool_size':         500,
        }

    @staticmethod
    def _equity_view(prices: pd.DataFrame) -> pd.DataFrame:
        """Equity-only columns on equity trading days (drops crypto/index/fx rows)."""
        cols = [t for t in prices.columns
                if isinstance(t, str) and not t.startswith('^')
                and '-USD' not in t and '=F' not in t and '=X' not in t]
        if not cols:
            return pd.DataFrame()
        return prices[cols].dropna(how='all')

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

        zwin  = int(self.parameters.get('z_window', self.Z_WINDOW))
        z_min = float(self.parameters.get('vol_z_min', self.VOL_Z_MIN))
        r_min = float(self.parameters.get('day_ret_min', self.DAY_RET_MIN))
        cd    = int(self.parameters.get('cooldown_sessions', self.COOLDOWN_SESSIONS))

        eq = self._equity_view(prices)
        if eq.empty or len(eq) < zwin + cd + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        pool = liquid_pool(eq, max_names=int(self.parameters.get('pool_size', 500)),
                           lookback=60)
        pool = [t for t in pool if t in universe] or pool
        if len(pool) < 10:
            print('[debug] signals=0', file=sys.stderr)
            return []

        vol = load_wide('volume', pool)
        if vol.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        asof = prices.index[-1]
        vol = vol.loc[:asof]

        common = [t for t in pool if t in eq.columns and t in vol.columns]
        if len(common) < 10:
            print('[debug] signals=0', file=sys.stderr)
            return []
        c = eq[common].iloc[-(zwin + cd + 2):].astype('float64')
        v = vol.reindex(c.index)[common].astype('float64')

        # z-score of today's volume vs the trailing zwin-day window ending
        # YESTERDAY (shift(1) keeps the day itself out of its own baseline).
        mu = v.shift(1).rolling(zwin, min_periods=self.MIN_VALID_DAYS).mean()
        sd = v.shift(1).rolling(zwin, min_periods=self.MIN_VALID_DAYS).std(ddof=0)
        z  = (v - mu) / sd.where(sd > 0)
        ret1 = c.pct_change()

        trigger = (z > z_min) & (ret1 > r_min)
        # Per-name weekly dedupe, deterministic from the index alone: skip any
        # name whose trigger ALSO fired in the previous `cd` sessions (the
        # signal would already have been emitted on that earlier bar).
        fired_recently = trigger.iloc[-(cd + 1):-1].any()
        today = trigger.iloc[-1] & ~fired_recently
        cand = z.iloc[-1].where(today).dropna().sort_values(ascending=False)

        scale = self.position_scale(regime_state)
        current = c.iloc[-1]

        def _conf(zv: float) -> str:
            if zv >= 4.00:
                return 'HIGH'
            if zv >= 3.25:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for ticker, zv in cand.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                continue
            price = float(raw)
            series = prices[ticker].dropna()
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
                confidence=_conf(float(zv)),
                signal_params={
                    'volume_z_60d':      round(float(zv), 4),
                    'day_return':        round(float(ret1.iloc[-1][ticker]), 4),
                    'cooldown_sessions': cd,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
