"""
Pre-Holiday Effect — LONG SPY two trading days before each NYSE holiday.

Source: Ariel (1990, JF) "High Stock Returns before Holidays"; Lakonishok &
Smidt (1988, RFS). Returns on the session(s) immediately preceding exchange
holidays are an order of magnitude above average — one of the most durable
calendar anomalies. ~9 holidays/yr.

Signal: fire LONG SPY when the current bar is 2 trading days before an NYSE
holiday (signal at s-2 close -> engine fills at the s-1 close, the classic
pre-holiday session, and holds over the holiday; exits via brackets/21d).

Holiday set is computed deterministically in-code (no network): the
pandas.tseries.holiday US federal rules MINUS Columbus Day and Veterans Day
(markets open) PLUS Good Friday — the NYSE observed-holiday set.
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd
from pandas.tseries.holiday import AbstractHolidayCalendar, GoodFriday, USFederalHolidayCalendar

from strategies.base import BaseStrategy, Signal

__all__ = ['PreholidayEffect']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_preholiday_effect'

SPY = 'SPY'


class _NyseHolidayCalendar(AbstractHolidayCalendar):
    """US federal rules minus Columbus/Veterans Day, plus Good Friday."""
    rules = [r for r in USFederalHolidayCalendar.rules
             if 'columbus' not in r.name.lower() and 'veterans' not in r.name.lower()
             ] + [GoodFriday]


_NYSE_CAL = _NyseHolidayCalendar()
_NYSE_CBD = pd.offsets.CustomBusinessDay(calendar=_NYSE_CAL)


class PreholidayEffect(BaseStrategy):
    """LONG SPY at the close 2 trading days before each NYSE holiday."""

    id                = STRATEGY_ID
    name              = 'Pre-Holiday Effect'
    description       = ('LONG SPY 2 trading days before each NYSE holiday (Ariel 1990; Lakonishok-Smidt '
                         '1988); deterministic in-code exchange-rule holiday set; exits via brackets/hold.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 20
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    BASE_SIZE   = 0.85    # single-instrument etp timer norm (0.5-0.95)
    SCAN_DAYS   = 15      # calendar-day horizon for the holiday lookup

    def default_parameters(self) -> dict:
        return {'base_size': self.BASE_SIZE}

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
        # Union-calendar guard: only act on bars that ARE equity sessions
        # (prevents duplicate fires from weekend crypto-only rows).
        if eq.empty or eq.index[-1] != prices.index[-1] or SPY not in eq.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []
        asof = pd.Timestamp(eq.index[-1]).normalize()

        # The current bar is "2 trading days before a holiday" when the NEXT
        # scheduled session (t1) is the LAST session before an observed
        # holiday — i.e. some holiday falls strictly between t1 and t2.
        try:
            t1 = asof + _NYSE_CBD
            t2 = t1 + _NYSE_CBD
            hols = _NYSE_CAL.holidays(start=asof, end=asof + pd.Timedelta(days=self.SCAN_DAYS))
        except Exception:
            print('[debug] signals=0', file=sys.stderr)
            return []
        upcoming = [h for h in hols if t1 < h < t2]
        if not upcoming:
            print('[debug] signals=0', file=sys.stderr)
            return []

        series = eq[SPY].dropna()
        if len(series) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []
        price = float(series.iloc[-1])
        if not np.isfinite(price) or price <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        stops = self.compute_stops_and_targets(series, 'LONG', price,
                                               regime_state=regime_state)
        scale = self.position_scale(regime_state)
        base  = float(self.parameters.get('base_size', self.BASE_SIZE))
        signals = [Signal(
            ticker=SPY,
            direction='LONG',
            entry_price=price,
            stop_loss=stops['stop'],
            target_1=stops['t1'],
            target_2=stops['t2'],
            target_3=stops['t3'],
            position_size_pct=round(base * scale, 6),
            confidence='MED',
            signal_params={
                'holiday':           str(pd.Timestamp(upcoming[0]).date()),
                'sessions_before':   2,
                'fill_session_note': 'signal s-2 close; engine fills s-1 close (pre-holiday session)',
            },
        )]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
