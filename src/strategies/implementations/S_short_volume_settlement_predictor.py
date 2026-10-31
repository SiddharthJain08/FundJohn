"""
Short-interest settlement-trend predictor — FINRA biweekly short interest.

Source: "Does Daily Short Volume Predict Short Interest? Evidence From
660,000 Settlement Windows" (equibles.com, 2026). The paper's own predictor
(a daily short-volume feed trained through a transformer) is not available
in this ledger — there is no daily short-volume series, only the FINRA
biweekly settlement itself (data/master/short_interest.parquet). This is a
clean-room re-implementation of the paper's trading rule using what the
ledger actually has: the realized settlement change (`change_pct`) is
cross-sectionally ranked at the moment it first becomes publicly knowable,
and the extremes are traded for continuation/reversal into the next
settlement window — SHORT the top decile (short interest surging, crowding
risk), LONG the bottom decile (short interest collapsing, covering tailwind).

Point-in-time: FINRA publishes each settlement ~9 business days after the
settlement date (documented in src/ingestion/ingest_finra_short_interest.py).
`available_at` = settlement_date + 9 business days; a reading is only used
on bars strictly after it becomes available. Fires once per ticker on the
first bar after a new settlement becomes available (event-driven, like
S_earnings_sue_pead), not every day it is outstanding.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['ShortVolumeSettlementPredictor']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_short_volume_settlement_predictor'

_ROOT = os.path.join(os.path.dirname(__file__), '..', '..', '..')
SHORT_INTEREST_PARQUET = os.path.abspath(
    os.path.join(_ROOT, 'data', 'master', 'short_interest.parquet'))

_PUBLISH_LAG_BDAYS = 9

_SI_CACHE: Optional[pd.DataFrame] = None


def _load_short_interest() -> pd.DataFrame:
    """short_interest.parquet -> DataFrame[ticker, settlement_date, available_at,
    change_pct], module-cached, column-pruned.

    `available_at` = settlement_date + 9 business days (FINRA's documented
    publication lag) — using settlement_date itself would leak look-ahead.
    """
    global _SI_CACHE
    if _SI_CACHE is not None:
        return _SI_CACHE
    if not os.path.isfile(SHORT_INTEREST_PARQUET):
        _SI_CACHE = pd.DataFrame()
        return _SI_CACHE
    try:
        df = pd.read_parquet(
            SHORT_INTEREST_PARQUET,
            columns=['ticker', 'settlement_date', 'change_pct'])
        df['settlement_date'] = pd.to_datetime(df['settlement_date'], errors='coerce')
        df = df.dropna(subset=['ticker', 'settlement_date', 'change_pct'])
        df['available_at'] = df['settlement_date'] + pd.tseries.offsets.BDay(_PUBLISH_LAG_BDAYS)
        _SI_CACHE = df[['ticker', 'settlement_date', 'available_at', 'change_pct']] \
            .sort_values('available_at').reset_index(drop=True)
    except Exception:
        _SI_CACHE = pd.DataFrame()
    return _SI_CACHE


class ShortVolumeSettlementPredictor(BaseStrategy):
    """Rank tickers by their newly-published settlement short-interest change;
    SHORT the top decile (SI surging), LONG the bottom decile (SI collapsing)."""

    id                = STRATEGY_ID
    name              = 'Short Volume Settlement Predictor'
    description       = ('Cross-sectional rank of newly-published FINRA short-interest '
                         'change_pct: SHORT top decile (crowding risk), LONG bottom decile '
                         '(short-covering tailwind). Point-in-time via a 9-bday publish lag.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 60
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 10

    DECILE_HI        = 0.90
    DECILE_LO        = 0.10
    MIN_CROSS_SECTION = 100
    PICK_COUNT       = 10
    BASE_SIZE_SHORT  = 0.012
    BASE_SIZE_LONG   = 0.015

    def default_parameters(self) -> dict:
        return {
            'decile_hi':  self.DECILE_HI,
            'decile_lo':  self.DECILE_LO,
            'pick_count': self.PICK_COUNT,
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

        events = _load_short_interest()
        if events.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

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

        # Point-in-time: only settlements already publicly known as of asof.
        pit = events[events['available_at'] <= asof]
        if pit.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        # Fire on the first bar after a settlement newly becomes available.
        fresh = pit[pit['available_at'] > prev_bar]
        if fresh.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        fresh = fresh.sort_values('available_at').drop_duplicates('ticker', keep='last')

        if len(fresh) < self.MIN_CROSS_SECTION:
            print('[debug] signals=0', file=sys.stderr)
            return []

        decile_hi = float(self.parameters.get('decile_hi', self.DECILE_HI))
        decile_lo = float(self.parameters.get('decile_lo', self.DECILE_LO))
        picks     = int(self.parameters.get('pick_count', self.PICK_COUNT))

        # Cross-sectional rank (robust to the outliers raw change_pct can
        # have near a near-zero prior short-interest base).
        rank_pct = fresh['change_pct'].rank(pct=True)
        fresh = fresh.assign(rank_pct=rank_pct)

        rows = []
        for r in fresh.itertuples(index=False):
            if r.rank_pct >= decile_hi:
                rows.append((r.ticker, 'SHORT', float(r.rank_pct), float(r.change_pct)))
            elif r.rank_pct <= decile_lo:
                rows.append((r.ticker, 'LONG', float(r.rank_pct), float(r.change_pct)))
        if not rows:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rows = [r for r in rows if r[0] in universe] or rows
        rows.sort(key=lambda x: (-abs(x[2] - 0.5), x[0]))
        rows = rows[:picks]

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]

        signals: List[Signal] = []
        for ticker, direction, rank_pct_val, change_pct in rows:
            if len(signals) >= self.MAX_SIGNALS:
                break
            if ticker not in eq.columns:
                continue
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                continue
            price = float(raw)
            extremity = abs(rank_pct_val - 0.5)
            confidence = 'HIGH' if extremity >= 0.47 else ('MED' if extremity >= 0.43 else 'LOW')
            base = self.BASE_SIZE_SHORT if direction == 'SHORT' else self.BASE_SIZE_LONG
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
                    'change_pct': round(change_pct, 4),
                    'rank_pct':   round(rank_pct_val, 4),
                    'cross_section_n': int(len(fresh)),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
