"""
No-News Momentum — momentum works best where information travels slowly.

Source: Hong, Lim & Stein (2000, Journal of Finance),
"Bad news travels slowly: Size, analyst coverage, and the profitability of
momentum strategies."

Hypothesis: momentum profits concentrate in low-attention names where
information diffuses gradually. Proxy attention with trailing 60-session news
coverage (sum of news_count_24h); within the LOWEST-coverage tercile of the
liquid pool, run classic 12-1 momentum: LONG the strongest, SHORT the
weakest. Monthly rebalance, <=10 per leg.

Data: close panel (engine) + news_count_24h HISTORY self-loaded from
sentiment.parquet. The engine aux_data['sentiment'] slice is a single-day
snapshot and cannot yield a 60d sum, so this strategy reads the sentiment
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

__all__ = ['NoNewsMomentum']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_no_news_momentum'

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


class NoNewsMomentum(BaseStrategy):
    """12-1 momentum long/short restricted to the lowest-news-coverage tercile."""

    id                = STRATEGY_ID
    name              = 'No-News Momentum'
    description       = ('Hong-Lim-Stein: 12-1 momentum long/short ONLY within the lowest tercile '
                         'of trailing 60d news coverage (sum news_count_24h); monthly, <=10/leg.')
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 273           # 252d momentum formation + 21d skip
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 20

    COVERAGE_DAYS   = 60
    MOM_LONG        = 252
    MOM_SKIP        = 21
    LEG_COUNT       = 10
    MIN_NEWS_ROWS   = 200             # window activity floor — else sentiment history absent
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

    def default_parameters(self) -> dict:
        return {
            'coverage_days': self.COVERAGE_DAYS,
            'leg_count':     self.LEG_COUNT,
            'pool_size':     500,
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
    def _month_boundary(idx: pd.DatetimeIndex) -> bool:
        if len(idx) < 2:
            return False
        return pd.Timestamp(idx[-1]).month != pd.Timestamp(idx[-2]).month

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
        if len(eq) < self.min_lookback or not (self._month_boundary(eq.index) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        p = self.parameters
        pool = liquid_pool(prices, max_names=int(p.get('pool_size', 500)))
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in eq.columns]
        if len(pool) < 60:
            print('[debug] signals=0', file=sys.stderr)
            return []

        cov_days = int(p.get('coverage_days', self.COVERAGE_DAYS))
        asof = pd.Timestamp(eq.index[-1])
        win_start = pd.Timestamp(eq.index[-cov_days])

        sent = _load_sentiment_long('news_count_24h')
        if sent.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        pool_set = set(pool)
        sl = sent[(sent['date'] >= win_start) & (sent['date'] <= asof)
                  & (sent['ticker'].isin(pool_set))]
        # Activity floor: pre-2022 bars (or a missing collection window) have no
        # news history — coverage terciles would be meaningless noise.
        if len(sl) < self.MIN_NEWS_ROWS:
            print('[debug] signals=0', file=sys.stderr)
            return []

        counts = sl.groupby('ticker', observed=True)['news_count_24h'].sum()
        coverage = pd.Series(0.0, index=pool, dtype='float64')
        coverage.update(counts.astype('float64'))

        # Lowest-coverage tercile (quantile cut is robust to zero-inflation).
        members = coverage[coverage <= coverage.quantile(1.0 / 3.0)].index.tolist()
        if len(members) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        c = eq[members].astype('float64')
        if len(c) < self.MOM_LONG + self.MOM_SKIP + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []
        mom = (c.iloc[-(self.MOM_SKIP + 1)] / c.iloc[-(self.MOM_LONG + 1)] - 1.0).dropna()
        mom = mom[np.isfinite(mom)]
        if len(mom) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        leg = int(p.get('leg_count', self.LEG_COUNT))
        rank_pct = mom.rank(pct=True)
        winners = mom.sort_values(ascending=False).head(leg)
        losers  = mom.sort_values(ascending=True).head(leg)

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]

        def _conf(pct: float, long: bool) -> str:
            ext = pct if long else (1.0 - pct)
            if ext >= 0.97:
                return 'HIGH'
            if ext >= 0.90:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for legser, direction, base in ((winners, 'LONG', self.BASE_SIZE_LONG),
                                        (losers, 'SHORT', self.BASE_SIZE_SHORT)):
            for ticker, m in legser.items():
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
                    confidence=_conf(float(rank_pct[ticker]), direction == 'LONG'),
                    signal_params={
                        'mom_12_1':      round(float(m), 4),
                        'news_count_60d': round(float(coverage.get(ticker, 0.0)), 1),
                        'tercile_size':  len(members),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
