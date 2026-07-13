"""
Quarter-End Rebalancing Flows — fade the QTD equity-bond spread into
quarter end.

Source: Etula, Rinne, Suominen & Vaittinen (2020, JF) "Dash for Cash";
institutional target-weight rebalancing. Pension/target-date mandates
rebalance to fixed equity/bond weights near quarter end: after a quarter
in which equities strongly outran bonds they must SELL equities / BUY
bonds (and vice versa), producing predictable price pressure over the last
sessions of the quarter that reverts after the turn.

Signal (fires at most once per quarter, ~2-4 trades/yr): on the 5th-from-
last scheduled session of the quarter, if QTD(SPY) - QTD(IEF) > +5% ->
SHORT SPY + LONG IEF; if < -5% -> LONG SPY + SHORT IEF. Exits via
brackets/21d max-hold (covers the quarter turn).

Data: close panel only (SPY + IEF). Returns [] gracefully when IEF is
absent from the panel. The end-of-quarter session countdown uses the same
deterministic in-code NYSE holiday rules as S_preholiday_effect.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd
from pandas.tseries.holiday import AbstractHolidayCalendar, GoodFriday, USFederalHolidayCalendar

from strategies.base import BaseStrategy, Signal

__all__ = ['QuarterEndRebalancingFlows']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_quarter_end_rebalancing_flows'

SPY = 'SPY'
IEF = 'IEF'


class _NyseHolidayCalendar(AbstractHolidayCalendar):
    """US federal rules minus Columbus/Veterans Day, plus Good Friday."""
    rules = [r for r in USFederalHolidayCalendar.rules
             if 'columbus' not in r.name.lower() and 'veterans' not in r.name.lower()
             ] + [GoodFriday]


_NYSE_CBD = pd.offsets.CustomBusinessDay(calendar=_NyseHolidayCalendar())


class QuarterEndRebalancingFlows(BaseStrategy):
    """Fade a stretched QTD SPY-IEF spread over the last sessions of a quarter."""

    id                = STRATEGY_ID
    name              = 'Quarter-End Rebalancing Flows'
    description       = ('QTD return spread SPY-IEF > +5% entering the last 5 sessions of a quarter -> '
                         'SHORT SPY + LONG IEF (institutional rebalancing pressure); mirror when < -5%. '
                         '~2-4 trades/yr.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    # Rebalancing flows are largest exactly in stressed quarters (2020-Q1) —
    # keep all four regimes and let the regime scale discount.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 2

    SPREAD_MIN     = 0.05    # |QTD(SPY) - QTD(IEF)| trigger
    SESSIONS_LEFT  = 4       # fire when exactly 4 scheduled sessions remain after t
    LEG_SIZE       = 0.50    # per leg (two-leg pair; etp timer norm)

    def default_parameters(self) -> dict:
        return {
            'spread_min': self.SPREAD_MIN,
            'leg_size':   self.LEG_SIZE,
        }

    @staticmethod
    def _equity_view(prices: pd.DataFrame) -> pd.DataFrame:
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

        eq = self._equity_view(prices)
        if eq.empty or eq.index[-1] != prices.index[-1]:
            print('[debug] signals=0', file=sys.stderr)
            return []
        if SPY not in eq.columns or IEF not in eq.columns:
            # Graceful when IEF (or SPY) is missing from the panel.
            print('[debug] signals=0', file=sys.stderr)
            return []
        asof = pd.Timestamp(eq.index[-1]).normalize()

        # Cadence: fire only on the 5th-from-last scheduled session of the
        # quarter (exactly SESSIONS_LEFT sessions remain after t) — a single
        # deterministic bar per quarter.
        q = pd.Period(asof, freq='Q')
        q_end = q.end_time.normalize()
        try:
            remaining = pd.date_range(asof + pd.Timedelta(days=1), q_end, freq=_NYSE_CBD)
        except Exception:
            print('[debug] signals=0', file=sys.stderr)
            return []
        if len(remaining) != self.SESSIONS_LEFT:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # QTD returns off the last equity session of the PRIOR quarter.
        q_start = q.start_time.normalize()
        prior = eq.index[eq.index < q_start]
        if len(prior) == 0:
            print('[debug] signals=0', file=sys.stderr)
            return []
        base_date = prior[-1]

        legs_px = {}
        for ticker in (SPY, IEF):
            series = eq[ticker].dropna()
            base_s = series.loc[:base_date]
            if base_s.empty or series.empty:
                print('[debug] signals=0', file=sys.stderr)
                return []
            base_px = float(base_s.iloc[-1])
            last_px = float(series.iloc[-1])
            if not (np.isfinite(base_px) and np.isfinite(last_px)) or base_px <= 0 or last_px <= 0:
                print('[debug] signals=0', file=sys.stderr)
                return []
            legs_px[ticker] = (series, base_px, last_px)

        qtd_spy = legs_px[SPY][2] / legs_px[SPY][1] - 1.0
        qtd_ief = legs_px[IEF][2] / legs_px[IEF][1] - 1.0
        spread  = qtd_spy - qtd_ief

        thr = float(self.parameters.get('spread_min', self.SPREAD_MIN))
        if spread > thr:
            legs = [(SPY, 'SHORT'), (IEF, 'LONG')]    # equities outran -> sell pressure on SPY
        elif spread < -thr:
            legs = [(SPY, 'LONG'), (IEF, 'SHORT')]
        else:
            print('[debug] signals=0', file=sys.stderr)
            return []

        conf = 'HIGH' if abs(spread) > 0.10 else 'MED'
        scale = self.position_scale(regime_state)
        leg_size = float(self.parameters.get('leg_size', self.LEG_SIZE))

        signals: List[Signal] = []
        for ticker, direction in legs:
            series, _, last_px = legs_px[ticker]
            stops = self.compute_stops_and_targets(series, direction, last_px,
                                                   regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=last_px,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(leg_size * scale, 6),
                confidence=conf,
                signal_params={
                    'qtd_spy':        round(float(qtd_spy), 4),
                    'qtd_ief':        round(float(qtd_ief), 4),
                    'spread':         round(float(spread), 4),
                    'sessions_left':  int(len(remaining)),
                    'quarter':        str(q),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
