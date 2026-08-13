"""
Implied Correlation Timing — SPY timing off the index-vs-names IV spread.

Source: Driessen, Maenhout & Vilkov (2009) correlation risk premium; the
CBOE Implied Correlation (ICJ/COR) family. Index implied variance equals
the weighted single-name implied variances TIMES the implied correlation,
so the ratio  SPY_iv30^2 / mean(single-name iv30^2)  is a tradable
implied-correlation proxy. Stretched-high implied correlation marks
systemic-fear pricing that mean-reverts (index vol richens vs names ->
fade the index); stretched-low marks complacency.

Signal (weekly, 3-day confirmation): proxy above its 80th percentile of
the trailing 252 observations for 3 consecutive observations -> SHORT SPY;
below the 20th percentile for 3 consecutive -> LONG SPY.

Data: aux_data['options'] enriched panel iv30 of SPY vs single names
(STALE — covers 2024-04-22 -> 2026-04-22; the per-day slice repeats after
that, which freezes the proxy and structurally disables new fires).
Percentile history is accumulated on the instance across bars (one
instance per backtest, chronological bars — same idiom as the batch's
per-name cooldown state). Returns [] gracefully when aux is absent.
"""
from __future__ import annotations

import sys
from typing import Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['ImpliedCorrelationTiming']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_implied_correlation_timing'

SPY = 'SPY'


class ImpliedCorrelationTiming(BaseStrategy):
    """Fade stretched implied correlation via SPY (SHORT high / LONG low)."""

    id                = STRATEGY_ID
    name              = 'Implied Correlation Timing'
    description       = ('Implied-corr proxy = SPY iv30^2 / mean single-name iv30^2; >80th pctile of '
                         'trailing 252 obs for 3 consecutive days -> SHORT SPY, <20th -> LONG SPY; weekly.')
    tier              = 3
    signal_frequency  = 'weekly'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    MIN_NAMES    = 30      # single names with valid iv30 required per bar
    HIST_WINDOW  = 252     # trailing proxy observations for the percentile
    MIN_HIST     = 60      # minimum observations before any fire
    HI_PCT       = 0.80
    LO_PCT       = 0.20
    CONFIRM_DAYS = 3
    FRESH_DAYS   = 7       # last 3 proxy obs must be this recent (calendar)
    BASE_SIZE    = 0.60    # single-instrument etp timer norm (0.5-0.95)

    def __init__(self, parameters: dict = None):
        super().__init__(parameters)
        # {asof Timestamp: proxy float} — accumulated chronologically, keyed
        # by date so re-runs of the same bar are idempotent.
        self._proxy_hist: Dict[pd.Timestamp, float] = {}

    def default_parameters(self) -> dict:
        return {
            'hi_pct':       self.HI_PCT,
            'lo_pct':       self.LO_PCT,
            'confirm_days': self.CONFIRM_DAYS,
            'min_names':    self.MIN_NAMES,
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
    def _week_boundary(index: pd.Index) -> bool:
        """True on the first trading day of an ISO week."""
        if len(index) < 2:
            return False
        d1, d0 = pd.Timestamp(index[-1]), pd.Timestamp(index[-2])
        return d1.isocalendar()[:2] != d0.isocalendar()[:2]

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

        eq = self._equity_view(prices)
        if eq.empty or eq.index[-1] != prices.index[-1] or SPY not in eq.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []
        asof = pd.Timestamp(eq.index[-1])

        opts = (aux_data or {}).get('options') or {}
        spy_opts = opts.get(SPY) if isinstance(opts.get(SPY), dict) else None

        # ── Accumulate the proxy EVERY computable bar (before any gating)
        # so the percentile window stays dense regardless of cadence/regime.
        min_names = int(self.parameters.get('min_names', self.MIN_NAMES))
        if spy_opts is not None:
            spy_iv = spy_opts.get('iv30')
            try:
                spy_iv = float(spy_iv) if spy_iv is not None else None
            except (TypeError, ValueError):
                spy_iv = None
            if spy_iv is not None and np.isfinite(spy_iv) and spy_iv > 0:
                name_vars = []
                for tkr, o in opts.items():
                    if tkr == SPY or not isinstance(o, dict) or not isinstance(tkr, str):
                        continue
                    if tkr.startswith('^'):
                        continue
                    iv = o.get('iv30')
                    try:
                        iv = float(iv) if iv is not None else None
                    except (TypeError, ValueError):
                        continue
                    if iv is not None and np.isfinite(iv) and iv > 0:
                        name_vars.append(iv * iv)
                if len(name_vars) >= min_names:
                    denom = float(np.mean(name_vars))
                    if denom > 0:
                        self._proxy_hist[asof] = (spy_iv * spy_iv) / denom

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if not (self._week_boundary(eq.index) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        dates = sorted(d for d in self._proxy_hist if d <= asof)
        if len(dates) < self.MIN_HIST:
            print('[debug] signals=0', file=sys.stderr)
            return []
        window_dates = dates[-self.HIST_WINDOW:]
        values = np.array([self._proxy_hist[d] for d in window_dates], dtype='float64')

        confirm = int(self.parameters.get('confirm_days', self.CONFIRM_DAYS))
        last_dates = window_dates[-confirm:]
        # Staleness guard: the confirming observations must be recent — a
        # frozen aux panel stops producing new dates and disables fires.
        if (asof - last_dates[0]).days > self.FRESH_DAYS:
            print('[debug] signals=0', file=sys.stderr)
            return []
        last_vals = values[-confirm:]

        hi = float(np.quantile(values, float(self.parameters.get('hi_pct', self.HI_PCT))))
        lo = float(np.quantile(values, float(self.parameters.get('lo_pct', self.LO_PCT))))

        direction = None
        if bool(np.all(last_vals > hi)):
            direction = 'SHORT'
        elif bool(np.all(last_vals < lo)):
            direction = 'LONG'
        if direction is None:
            print('[debug] signals=0', file=sys.stderr)
            return []

        series = eq[SPY].dropna()
        if series.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        price = float(series.iloc[-1])
        if not np.isfinite(price) or price <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        extreme_hi = float(np.quantile(values, 0.90))
        extreme_lo = float(np.quantile(values, 0.10))
        if direction == 'SHORT':
            conf = 'HIGH' if bool(np.all(last_vals > extreme_hi)) else 'MED'
        else:
            conf = 'HIGH' if bool(np.all(last_vals < extreme_lo)) else 'MED'

        stops = self.compute_stops_and_targets(series, direction, price,
                                               regime_state=regime_state)
        scale = self.position_scale(regime_state)
        signals = [Signal(
            ticker=SPY,
            direction=direction,
            entry_price=price,
            stop_loss=stops['stop'],
            target_1=stops['t1'],
            target_2=stops['t2'],
            target_3=stops['t3'],
            position_size_pct=round(self.BASE_SIZE * scale, 6),
            confidence=conf,
            signal_params={
                'implied_corr_proxy': round(float(last_vals[-1]), 4),
                'pctile_hi':          round(hi, 4),
                'pctile_lo':          round(lo, 4),
                'hist_len':           len(values),
                'n_single_names':     min_names,
            },
        )]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
