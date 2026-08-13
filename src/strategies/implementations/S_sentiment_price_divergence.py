"""
Sentiment-Price Divergence — delayed incorporation of shifting news tone.

Hypothesis: when the TREND of news sentiment (20-session slope of FinBERT
news_mean_score, z-scored against its own trailing distribution) improves
sharply while price has gone nowhere or down over the same 20 sessions, the
market is late incorporating the tone shift -> LONG. Mirror SHORT when
sentiment tone deteriorates sharply while price is still up. Weekly cadence,
<=8 per leg.

Data: close panel (engine) + news_mean_score HISTORY self-loaded from
sentiment.parquet. The engine aux_data['sentiment'] slice is a single-day
snapshot and cannot yield a 20d slope, so this strategy reads the sentiment
parquet directly (column-pruned, date-floored 2022+, module-cached — same
memory discipline as _extra_panels; news_* columns are populated 2022->,
social_* columns are empty and never used). Point-in-time: rows <= asof only.
"""
from __future__ import annotations

import os
import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import liquid_pool

__all__ = ['SentimentPriceDivergence']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_sentiment_price_divergence'

_ROOT = os.path.join(os.path.dirname(__file__), '..', '..', '..')
SENTIMENT_PARQUET = os.path.abspath(os.path.join(_ROOT, 'data', 'master', 'sentiment.parquet'))

_SENT_CACHE: dict = {}


def _load_sentiment_long(field: str, date_floor: str = '2022-01-01') -> pd.DataFrame:
    """Long-format [ticker, date, field] rows >= date_floor. Cached; empty on failure."""
    key = (field, date_floor)
    hit = _SENT_CACHE.get(key)
    if hit is not None:
        return hit
    if not os.path.isfile(SENTIMENT_PARQUET):
        _SENT_CACHE[key] = pd.DataFrame()
        return _SENT_CACHE[key]
    try:
        df = pd.read_parquet(SENTIMENT_PARQUET, columns=['ticker', 'date', field],
                             filters=[('date', '>=', date_floor)])
        df['date'] = pd.to_datetime(df['date'])
        df[field] = pd.to_numeric(df[field], errors='coerce').astype('float32')
        df = df.dropna(subset=[field])
    except Exception:
        df = pd.DataFrame()
    _SENT_CACHE[key] = df
    return df


class SentimentPriceDivergence(BaseStrategy):
    """LONG improving-sentiment/flat-price divergence; SHORT the mirror."""

    id                = STRATEGY_ID
    name              = 'Sentiment-Price Divergence'
    description       = ('20d news_mean_score slope z > +1 while 20d price return < 0 -> LONG '
                         '(delayed incorporation); mirror SHORT; weekly, <=8/leg.')
    tier              = 3
    signal_frequency  = 'weekly'
    min_lookback      = 150
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 16

    SLOPE_DAYS      = 20
    Z_WINDOW        = 60
    RET_DAYS        = 20
    Z_MIN           = 1.0
    MIN_OBS_20D     = 10              # raw news-score days required in the slope window
    LEG_COUNT       = 8
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

    def default_parameters(self) -> dict:
        return {
            'slope_days': self.SLOPE_DAYS,
            'z_min':      self.Z_MIN,
            'leg_count':  self.LEG_COUNT,
            'pool_size':  500,
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

    @staticmethod
    def _week_boundary(idx: pd.DatetimeIndex) -> bool:
        """True on the first trading day of an ISO week (weekly cadence gate)."""
        if len(idx) < 2:
            return False
        i1 = pd.Timestamp(idx[-1]).isocalendar()
        i0 = pd.Timestamp(idx[-2]).isocalendar()
        return (i1.week != i0.week) or (i1.year != i0.year)

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

        eq = self._equity_rows(prices)
        if len(eq) < self.min_lookback or not (self._week_boundary(eq.index) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        pool = liquid_pool(prices, max_names=int(p.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in eq.columns]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        slope_days = int(p.get('slope_days', self.SLOPE_DAYS))
        tail = slope_days + self.Z_WINDOW + 20
        idx  = eq.index[-tail:]
        asof = pd.Timestamp(idx[-1])

        sent = _load_sentiment_long('news_mean_score')
        if sent.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        pool_set = set(pool)
        sl = sent[(sent['date'] >= pd.Timestamp(idx[0])) & (sent['date'] <= asof)
                  & (sent['ticker'].isin(pool_set))]
        if len(sl) < 200:
            print('[debug] signals=0', file=sys.stderr)
            return []

        s = sl.pivot_table(index='date', columns='ticker', values='news_mean_score',
                           aggfunc='last').reindex(idx)

        # Density gate: >= MIN_OBS_20D raw score days in the slope window; then a
        # short forward-fill (2d) bridges weekend/holiday gaps only.
        raw_obs = s.iloc[-slope_days:].notna().sum()
        keep = raw_obs[raw_obs >= self.MIN_OBS_20D].index.tolist()
        if not keep:
            print('[debug] signals=0', file=sys.stderr)
            return []
        s = s[keep].ffill(limit=2).astype('float64')

        # 20-session OLS slope as a fixed FIR filter: slope_t = sum_i w_i * y_{t-19+i}.
        x = np.arange(slope_days, dtype='float64')
        w = x - x.mean()
        w = w / (w ** 2).sum()
        slope = None
        for i in range(slope_days):
            term = s.shift(slope_days - 1 - i) * w[i]
            slope = term if slope is None else slope + term

        zm = slope.rolling(self.Z_WINDOW, min_periods=30).mean()
        zs = slope.rolling(self.Z_WINDOW, min_periods=30).std()
        with np.errstate(divide='ignore', invalid='ignore'):
            z = (slope - zm) / zs
        z_now = z.iloc[-1].dropna()
        if z_now.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        c = eq[keep].astype('float64')
        ret20 = (c.iloc[-1] / c.iloc[-(self.RET_DAYS + 1)] - 1.0)

        z_min = float(p.get('z_min', self.Z_MIN))
        longs, shorts = [], []
        for t, zv in z_now.items():
            r = ret20.get(t)
            if r is None or not np.isfinite(r) or not np.isfinite(zv):
                continue
            if zv > z_min and r < 0:
                longs.append((t, float(zv), float(r)))
            elif zv < -z_min and r > 0:
                shorts.append((t, float(zv), float(r)))

        leg = int(p.get('leg_count', self.LEG_COUNT))
        longs.sort(key=lambda x: abs(x[1]), reverse=True)
        shorts.sort(key=lambda x: abs(x[1]), reverse=True)
        longs, shorts = longs[:leg], shorts[:leg]

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]

        def _conf(zv: float) -> str:
            if abs(zv) >= 2.0:
                return 'HIGH'
            if abs(zv) >= 1.5:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for legrows, direction, base in ((longs, 'LONG', self.BASE_SIZE_LONG),
                                         (shorts, 'SHORT', self.BASE_SIZE_SHORT)):
            for ticker, zv, r in legrows:
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
                    confidence=_conf(zv),
                    signal_params={
                        'sent_slope_z': round(zv, 4),
                        'ret_20d':      round(r, 4),
                        'news_obs_20d': int(raw_obs.get(ticker, 0)),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
