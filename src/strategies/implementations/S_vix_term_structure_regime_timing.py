"""
VIX Term-Structure Regime Timing — full-curve contango/backwardation timer.

Source: Simon & Campasano (2014) "The VIX Futures Basis"; the vol
term-structure carry literature. When the WHOLE short-end curve is in
contango (VIX9D < VIX < VIX3M), vol carry favors risk assets — LONG SPY.
When the whole curve inverts (backwardation), stress is being priced NOW;
backwardation episodes mean-revert violently, so rather than shorting SPY
the book rotates into the defensive pair XLP + XLU.

Signal (edge-triggered): VIX9D/VIX and VIX/VIX3M both < 0.95 for 3
consecutive macro observations (with the prior observation NOT qualifying)
-> LONG SPY; both > 1.02 for 3 consecutive -> LONG XLP + XLU. A 10-trading
-day cooldown suppresses a fresh edge if any edge (either leg) fired within
the prior 10 observations. Fully stateless — edges and the cooldown are
recomputed from the point-in-time macro series each bar.

Data: aux_data['macro'] dict of pd.Series (VIX, VIX3M, VIX9D — VIX9D
history starts 2021-05). Returns [] gracefully when macro is absent or the
latest macro observation is not the current bar (staleness guard, prevents
re-firing the same edge).
"""
from __future__ import annotations

import sys
from typing import List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['VixTermStructureRegimeTiming']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_vix_term_structure_regime_timing'

SPY = 'SPY'
DEFENSIVE_PAIR = ('XLP', 'XLU')


class VixTermStructureRegimeTiming(BaseStrategy):
    """Contango -> LONG SPY; backwardation -> LONG XLP+XLU (no SPY short)."""

    id                = STRATEGY_ID
    name              = 'VIX Term Structure Regime Timing'
    description       = ('VIX9D/VIX and VIX/VIX3M both <0.95 for 3 days (full contango) -> LONG SPY; '
                         'both >1.02 for 3 days (backwardation) -> LONG XLP+XLU defensive pair; '
                         'edge-triggered with 10d cooldown.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 20
    # Backwardation fires exactly in stress — keep all four regimes and let
    # the regime position scale do the discounting (commodity-ETP precedent).
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 2

    CONTANGO_MAX  = 0.95
    BACKWARD_MIN  = 1.02
    CONFIRM_DAYS  = 3
    COOLDOWN_DAYS = 10
    SPY_SIZE      = 0.75    # single-instrument etp timer norm (0.5-0.95)
    DEF_SIZE      = 0.375   # per defensive leg (pair sums to 0.75)

    def default_parameters(self) -> dict:
        return {
            'contango_max':  self.CONTANGO_MAX,
            'backward_min':  self.BACKWARD_MIN,
            'confirm_days':  self.CONFIRM_DAYS,
            'cooldown_days': self.COOLDOWN_DAYS,
        }

    @staticmethod
    def _equity_view(prices: pd.DataFrame) -> pd.DataFrame:
        cols = [t for t in prices.columns
                if isinstance(t, str) and not t.startswith('^')
                and '-USD' not in t and '=F' not in t and '=X' not in t]
        if not cols:
            return pd.DataFrame()
        return prices[cols].dropna(how='all')

    @staticmethod
    def _series(macro: dict, name: str) -> Optional[pd.Series]:
        s = macro.get(name)
        if not isinstance(s, pd.Series) or s.empty:
            return None
        s = s.dropna()
        return s if len(s) else None

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

        eq = self._equity_view(prices)
        if eq.empty or eq.index[-1] != prices.index[-1]:
            print('[debug] signals=0', file=sys.stderr)
            return []
        asof = pd.Timestamp(eq.index[-1]).normalize()

        macro = (aux_data or {}).get('macro') or {}
        vix   = self._series(macro, 'VIX')
        vix3m = self._series(macro, 'VIX3M')
        vix9d = self._series(macro, 'VIX9D')
        if vix is None or vix3m is None or vix9d is None:
            print('[debug] signals=0', file=sys.stderr)
            return []

        confirm  = int(self.parameters.get('confirm_days', self.CONFIRM_DAYS))
        cooldown = int(self.parameters.get('cooldown_days', self.COOLDOWN_DAYS))
        c_max    = float(self.parameters.get('contango_max', self.CONTANGO_MAX))
        b_min    = float(self.parameters.get('backward_min', self.BACKWARD_MIN))

        curve = pd.concat({'v9': vix9d, 'v30': vix, 'v3m': vix3m},
                          axis=1, join='inner').dropna()
        curve = curve.loc[:asof]
        need = confirm + cooldown + 2
        if len(curve) < need:
            print('[debug] signals=0', file=sys.stderr)
            return []
        # Staleness guard: only act when the macro curve has THIS bar's
        # observation — otherwise the same terminal edge would re-fire on
        # every subsequent bar of a frozen series.
        if pd.Timestamp(curve.index[-1]).normalize() != asof:
            print('[debug] signals=0', file=sys.stderr)
            return []
        curve = curve.iloc[-(need + confirm):]

        with np.errstate(divide='ignore', invalid='ignore'):
            rs = curve['v9'] / curve['v30']    # short-end slope
            rl = curve['v30'] / curve['v3m']   # long-end slope
        cont = ((rs < c_max) & (rl < c_max)).to_numpy()
        back = ((rs > b_min) & (rl > b_min)).to_numpy()

        def edge_at(cond: np.ndarray, i: int) -> bool:
            if i < confirm:                    # need a non-qualifying prior day
                return False
            return bool(cond[i - confirm + 1: i + 1].all() and not cond[i - confirm])

        t = len(curve) - 1
        fire_cont = edge_at(cont, t)
        fire_back = edge_at(back, t)
        if not (fire_cont or fire_back):
            print('[debug] signals=0', file=sys.stderr)
            return []
        for j in range(max(confirm, t - cooldown), t):
            if edge_at(cont, j) or edge_at(back, j):
                print('[debug] signals=0', file=sys.stderr)
                return []

        rs_last = float(rs.iloc[-1]); rl_last = float(rl.iloc[-1])
        if fire_cont:
            legs = [(SPY, self.SPY_SIZE)]
            conf = 'HIGH' if (rs_last < 0.90 and rl_last < 0.90) else 'MED'
            state_tag = 'contango'
        else:
            legs = [(tkr, self.DEF_SIZE) for tkr in DEFENSIVE_PAIR]
            conf = 'HIGH' if (rs_last > 1.08 and rl_last > 1.08) else 'MED'
            state_tag = 'backwardation'

        scale = self.position_scale(regime_state)
        signals: List[Signal] = []
        for ticker, base in legs:
            if ticker not in eq.columns:
                continue
            series = eq[ticker].dropna()
            if series.empty:
                continue
            price = float(series.iloc[-1])
            if not np.isfinite(price) or price <= 0:
                continue
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
                position_size_pct=round(base * scale, 6),
                confidence=conf,
                signal_params={
                    'curve_state':   state_tag,
                    'vix9d_vix':     round(rs_last, 4),
                    'vix_vix3m':     round(rl_last, 4),
                    'confirm_days':  confirm,
                    'cooldown_days': cooldown,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
