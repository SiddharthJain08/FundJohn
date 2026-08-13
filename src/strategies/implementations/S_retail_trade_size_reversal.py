"""
Retail Trade-Size Reversal — average-print-size shifts against extended moves.

Source: manual://fable-research/2026-07-12/S_retail_trade_size_reversal
(microstructure literature on trade-size clienteles: small prints proxy
retail order flow — Barber-Odean retail herding; Hvidkjaer 2008).

Hypothesis: average trade size = volume / transactions. A sharp DROP in
average print size while a stock is up strongly means the marginal buyer has
rotated from institutions to small retail orders — the classic late-stage
herding footprint — and the move reverts: SHORT. Mirror: a sharp RISE in
average print size into a hard sell-off means institutions are absorbing
retail panic: LONG. Gates: 20d z-score of the daily avg-trade-size change
beyond +/-1.5 AND 10d return beyond +/-8%. Weekly cadence, <=10 per leg.

Novel use of the `transactions` column of prices.parquet (unused elsewhere).

Data: close panel (engine) + self-loaded VOLUME and TRANSACTIONS panels from
prices.parquet via _extra_panels (point-in-time sliced, 2021+ coverage).
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

__all__ = ['RetailTradeSizeReversal']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_retail_trade_size_reversal'


class RetailTradeSizeReversal(BaseStrategy):
    """SHORT retail-herded rallies, LONG institution-absorbed sell-offs."""

    id                = STRATEGY_ID
    name              = 'Retail Trade-Size Reversal'
    description       = ('Avg trade size = volume/transactions; 20d z of its daily change < -1.5 on a +8% '
                         '10d run -> SHORT reversal (retail herding); mirror LONG on rising prints into -8%. '
                         'Weekly, <=10/leg.')
    tier              = 2
    signal_frequency  = 'weekly'
    min_lookback      = 90
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 20

    Z_WINDOW        = 20
    MIN_VALID_DAYS  = 14
    Z_THRESH        = 1.5
    RET_WINDOW      = 10
    RET_THRESH      = 0.08
    LEG_COUNT       = 10
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

    def default_parameters(self) -> dict:
        return {
            'z_window':   self.Z_WINDOW,
            'z_thresh':   self.Z_THRESH,
            'ret_window': self.RET_WINDOW,
            'ret_thresh': self.RET_THRESH,
            'leg_count':  self.LEG_COUNT,
            'pool_size':  500,
        }

    def _week_boundary(self, prices: pd.DataFrame) -> bool:
        """True on the first trading day of an ISO week (weekly cadence gate)."""
        if len(prices) < 2:
            return False
        a = pd.Timestamp(prices.index[-1]).isocalendar()
        b = pd.Timestamp(prices.index[-2]).isocalendar()
        return (a[0], a[1]) != (b[0], b[1])

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

        if not (self._week_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        zwin  = int(self.parameters.get('z_window', self.Z_WINDOW))
        z_thr = float(self.parameters.get('z_thresh', self.Z_THRESH))
        rwin  = int(self.parameters.get('ret_window', self.RET_WINDOW))
        r_thr = float(self.parameters.get('ret_thresh', self.RET_THRESH))
        n_leg = int(self.parameters.get('leg_count', self.LEG_COUNT))

        pool = liquid_pool(prices, max_names=int(self.parameters.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in prices.columns]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Union-calendar safety: equity sessions only (see liquid_pool docstring).
        eq = prices[pool].dropna(how='all')
        need = zwin + rwin + 4
        if len(eq) < need:
            print('[debug] signals=0', file=sys.stderr)
            return []

        vol = load_wide('volume', pool)
        trn = load_wide('transactions', pool)
        if vol.empty or trn.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        asof = prices.index[-1]

        c = eq.iloc[-need:].astype('float64')
        v = vol.loc[:asof].reindex(c.index)
        t = trn.loc[:asof].reindex(c.index)
        common = [x for x in pool if x in v.columns and x in t.columns]
        if len(common) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []
        c = c[common]
        v = v[common].astype('float64')
        t = t[common].astype('float64')

        ats = v / t.where(t > 0)                       # average trade size (shares/print)
        chg = ats.pct_change()
        hist = chg.iloc[:-1].iloc[-zwin:]              # trailing window ending yesterday
        valid = hist.notna().sum() >= self.MIN_VALID_DAYS
        mu = hist.mean()
        sd = hist.std(ddof=0)
        z  = ((chg.iloc[-1] - mu) / sd.where(sd > 0)).where(valid)

        ret_n = c.pct_change(rwin).iloc[-1]

        shorts = z.where((z < -z_thr) & (ret_n > r_thr)).dropna()
        longs  = z.where((z > z_thr) & (ret_n < -r_thr)).dropna()
        shorts = shorts.abs().sort_values(ascending=False).head(n_leg)
        longs  = longs.abs().sort_values(ascending=False).head(n_leg)

        scale = self.position_scale(regime_state)
        current = c.iloc[-1]

        def _conf(abs_z: float) -> str:
            if abs_z >= 2.5:
                return 'HIGH'
            if abs_z >= 2.0:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for leg, direction, base in ((shorts, 'SHORT', self.BASE_SIZE_SHORT),
                                     (longs, 'LONG', self.BASE_SIZE_LONG)):
            for ticker, abs_z in leg.items():
                if len(signals) >= self.MAX_SIGNALS:
                    break
                raw = current.get(ticker)
                if raw is None or not np.isfinite(raw) or raw <= 0:
                    continue
                price = float(raw)
                series = prices[ticker].dropna()
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
                    confidence=_conf(float(abs_z)),
                    signal_params={
                        'ats_change_z': round(float(z[ticker]), 4),
                        'ret_10d':      round(float(ret_n[ticker]), 4),
                        'avg_trade_size': round(float(ats.iloc[-1][ticker]), 2)
                                          if np.isfinite(ats.iloc[-1][ticker]) else None,
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
