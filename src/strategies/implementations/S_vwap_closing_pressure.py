"""
VWAP Closing Pressure — persistent close-vs-VWAP location as an accumulation tell.

Source: manual://fable-research/2026-07-12/S_vwap_closing_pressure
(execution-microstructure logic: institutional buy programs benchmarked to
VWAP lift late prints, so persistent above-VWAP closes fingerprint multi-day
accumulation campaigns; the mirror fingerprints distribution).

Hypothesis: score each liquid name by the 10-day mean of (close - vwap)/vwap.
Names that keep CLOSING above their session VWAP are being accumulated into
the close (demand exceeds the day's average clearing price) and drift higher;
names persistently closing below VWAP are under distribution. LONG the top
decile, SHORT the bottom decile, refreshed every 2 weeks. Names lacking vwap
coverage are excluded by construction (>=8 of 10 valid sessions required).

Data: close panel (engine) + self-loaded VWAP panel from prices.parquet via
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

__all__ = ['VwapClosingPressure']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_vwap_closing_pressure'


class VwapClosingPressure(BaseStrategy):
    """LONG persistent above-VWAP closers, SHORT persistent below-VWAP closers."""

    id                = STRATEGY_ID
    name              = 'VWAP Closing Pressure'
    description       = ('10d mean of (close-vwap)/vwap: LONG top decile (accumulation into the close), '
                         'SHORT bottom decile (distribution); refreshed every 2 weeks, <=10/leg.')
    tier              = 2
    signal_frequency  = 'weekly'    # actual gate fires every 2nd ISO week (see _fortnight_boundary)
    min_lookback      = 90
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 20

    PRESSURE_DAYS   = 10
    MIN_VALID_DAYS  = 8
    LEG_COUNT       = 10
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

    def default_parameters(self) -> dict:
        return {
            'pressure_days': self.PRESSURE_DAYS,
            'leg_count':     self.LEG_COUNT,
            'pool_size':     500,
        }

    def _fortnight_boundary(self, prices: pd.DataFrame) -> bool:
        """True on the first trading day of an EVEN ISO week (2-week cadence,
        deterministic from the index alone)."""
        if len(prices) < 2:
            return False
        a = pd.Timestamp(prices.index[-1]).isocalendar()
        b = pd.Timestamp(prices.index[-2]).isocalendar()
        return (a[0], a[1]) != (b[0], b[1]) and int(a[1]) % 2 == 0

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

        if not (self._fortnight_boundary(prices) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        pdays = int(self.parameters.get('pressure_days', self.PRESSURE_DAYS))
        n_leg = int(self.parameters.get('leg_count', self.LEG_COUNT))

        pool = liquid_pool(prices, max_names=int(self.parameters.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in prices.columns]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Union-calendar safety: equity sessions only.
        eq = prices[pool].dropna(how='all')
        if len(eq) < pdays + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        vwap = load_wide('vwap', pool)
        if vwap.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        asof = prices.index[-1]

        c = eq.iloc[-pdays:].astype('float64')
        w = vwap.loc[:asof].reindex(c.index)
        common = [t for t in pool if t in w.columns]
        if len(common) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []
        c = c[common]
        w = w[common].astype('float64')

        press = (c - w) / w.where(w > 0)
        valid = press.notna().sum() >= self.MIN_VALID_DAYS   # excludes thin-vwap names
        score = press.mean().where(valid).dropna()
        if len(score) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rank_pct = score.rank(pct=True)
        winners = rank_pct[rank_pct >= 0.90].sort_values(ascending=False).head(n_leg)
        losers  = rank_pct[rank_pct <= 0.10].sort_values(ascending=True).head(n_leg)

        scale = self.position_scale(regime_state)
        current = c.iloc[-1]

        def _conf(pct: float, long: bool) -> str:
            ext = pct if long else (1.0 - pct)
            if ext >= 0.97:
                return 'HIGH'
            if ext >= 0.93:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for leg, direction, base in ((winners, 'LONG', self.BASE_SIZE_LONG),
                                     (losers, 'SHORT', self.BASE_SIZE_SHORT)):
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
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=price,
                    stop_loss=stops['stop'],
                    target_1=stops['t1'],
                    target_2=stops['t2'],
                    target_3=stops['t3'],
                    position_size_pct=round(base * scale, 6),
                    confidence=_conf(float(rp), direction == 'LONG'),
                    signal_params={
                        'vwap_pressure_10d': round(float(score[ticker]), 5),
                        'rank_pct':          round(float(rp), 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
