"""
Earnings SUE PEAD — post-earnings-announcement drift on TRUE analyst
estimate surprises.

The only PEAD in the book using real analyst estimates: SUE =
(eps_actual - eps_estimated) / |eps_estimated| from
data/master/earnings.parquet (small file, module-cached, column-pruned).
Coverage: actual+estimated pairs start 2025-03 — bars before that degrade
to [] naturally via the point-in-time filter.

Entry (daily, event-driven, max 10/day):
  - the ticker's report date falls in (previous equity bar, current bar]
    — i.e. we act on the FIRST trading bar after the report (within the
    spec's 3-day post-report window; firing only on the first bar prevents
    the same event re-firing for 3 consecutive bars). Point-in-time: only
    reports with report date <= asof are ever visible.
  - LONG when SUE > +10% AND in the top quintile of the trailing-63-bar
    point-in-time SUE distribution; SHORT when SUE < -10% AND in the
    bottom quintile (threshold-only when the trailing distribution is
    too thin, < 20 observations).
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['EarningsSuePead']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_earnings_sue_pead'

_ROOT = os.path.join(os.path.dirname(__file__), '..', '..', '..')
EARNINGS_PARQUET = os.path.abspath(os.path.join(_ROOT, 'data', 'master', 'earnings.parquet'))

_EARNINGS_CACHE: Optional[pd.DataFrame] = None


def _load_earnings() -> pd.DataFrame:
    """earnings.parquet -> DataFrame[ticker, date, sue], module-cached.

    Small file (~2k rows); column-pruned direct read is safe on the VPS.
    Rows without BOTH eps_actual and eps_estimated (or with |estimate| too
    close to 0 for a stable ratio) are dropped. Empty frame on any failure.
    """
    global _EARNINGS_CACHE
    if _EARNINGS_CACHE is not None:
        return _EARNINGS_CACHE
    if not os.path.isfile(EARNINGS_PARQUET):
        _EARNINGS_CACHE = pd.DataFrame()
        return _EARNINGS_CACHE
    try:
        df = pd.read_parquet(EARNINGS_PARQUET,
                             columns=['ticker', 'date', 'eps_actual', 'eps_estimated'])
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.dropna(subset=['ticker', 'date', 'eps_actual', 'eps_estimated'])
        df = df[df['eps_estimated'].abs() >= 0.01]      # avoid near-zero-divisor blowups
        df = df.assign(
            sue=(df['eps_actual'] - df['eps_estimated']) / df['eps_estimated'].abs()
        ).replace([np.inf, -np.inf], np.nan).dropna(subset=['sue'])
        _EARNINGS_CACHE = df[['ticker', 'date', 'sue']].sort_values('date').reset_index(drop=True)
    except Exception:
        _EARNINGS_CACHE = pd.DataFrame()
    return _EARNINGS_CACHE


class EarningsSuePead(BaseStrategy):
    """LONG big positive true-SUE surprises, SHORT big negative, first bar post-report."""

    id                = STRATEGY_ID
    name              = 'Earnings SUE PEAD'
    description       = ('True SUE = (eps_actual - eps_estimated)/|eps_estimated| from '
                         'earnings.parquet; first bar after report: LONG SUE > +10% in the '
                         'trailing top quintile, SHORT < -10% in the bottom quintile. Max 10/day.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 10

    SUE_LONG_MIN     = 0.10
    SUE_SHORT_MAX    = -0.10
    TRAIL_BARS       = 63      # trailing SUE-distribution window (trading bars)
    MIN_TRAIL_OBS    = 20
    PICK_COUNT       = 10
    BASE_SIZE_LONG   = 0.015
    BASE_SIZE_SHORT  = 0.012

    def default_parameters(self) -> dict:
        return {
            'sue_long_min':  self.SUE_LONG_MIN,
            'sue_short_max': self.SUE_SHORT_MAX,
            'trail_bars':    self.TRAIL_BARS,
            'pick_count':    self.PICK_COUNT,
        }

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

        events = _load_earnings()
        if events.empty:
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

        asof = pd.Timestamp(eq.index[-1]).normalize()
        prev_bar = pd.Timestamp(eq.index[-2]).normalize()

        # Point-in-time: reports with date <= asof only. Fire on the FIRST
        # bar after the report: report date in (prev_bar, asof].
        pit = events[events['date'] <= asof]
        if pit.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        fresh = pit[pit['date'] > prev_bar]
        if fresh.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        # One event per ticker per bar (keep the most recent report).
        fresh = fresh.sort_values('date').drop_duplicates('ticker', keep='last')

        trail_bars = int(self.parameters.get('trail_bars', self.TRAIL_BARS))
        long_min   = float(self.parameters.get('sue_long_min', self.SUE_LONG_MIN))
        short_max  = float(self.parameters.get('sue_short_max', self.SUE_SHORT_MAX))
        picks      = int(self.parameters.get('pick_count', self.PICK_COUNT))

        # Trailing point-in-time SUE distribution for quintile placement.
        if len(eq) > trail_bars:
            trail_start = pd.Timestamp(eq.index[-(trail_bars + 1)])
        else:
            trail_start = pd.Timestamp(eq.index[0])
        trail = pit[pit['date'] > trail_start]['sue']
        if len(trail) >= self.MIN_TRAIL_OBS:
            q_hi = float(trail.quantile(0.80))
            q_lo = float(trail.quantile(0.20))
        else:
            q_hi, q_lo = -np.inf, np.inf     # threshold-only fallback (thin history)

        rows = []
        for r in fresh.itertuples(index=False):
            sue = float(r.sue)
            if sue > long_min and sue >= q_hi:
                rows.append((r.ticker, 'LONG', sue))
            elif sue < short_max and sue <= q_lo:
                rows.append((r.ticker, 'SHORT', sue))
        if not rows:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Soft universe filter (template idiom), then biggest absolute
        # surprises first; cap per day.
        rows = [r for r in rows if r[0] in universe] or rows
        rows.sort(key=lambda x: (-abs(x[2]), x[0]))
        rows = rows[:picks]

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]

        signals: List[Signal] = []
        for ticker, direction, sue in rows:
            if len(signals) >= self.MAX_SIGNALS:
                break
            if ticker not in eq.columns:
                continue
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                continue
            price = float(raw)
            a = abs(sue)
            confidence = 'HIGH' if a >= 0.50 else ('MED' if a >= 0.25 else 'LOW')
            base = self.BASE_SIZE_LONG if direction == 'LONG' else self.BASE_SIZE_SHORT
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
                    'sue':            round(sue, 4),
                    'trail_q80':      round(q_hi, 4) if np.isfinite(q_hi) else None,
                    'trail_q20':      round(q_lo, 4) if np.isfinite(q_lo) else None,
                    'trail_obs':      int(len(trail)),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
