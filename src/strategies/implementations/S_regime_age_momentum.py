"""
Regime-Age Momentum — regime lifecycle conditions the cross-sectional edge.

Hypothesis: freshly-established volatility regimes (age <= 10 trading days)
mark the start of a new market phase where trend continuation dominates —
6-month momentum works. A long-aged LOW_VOL regime (> 60 days of calm) is a
range-bound chop environment where short-term mean-reversion dominates —
buy the 5-day losers instead.

The engine's regime dict does NOT carry days_in_current_state reliably, so
the regime state AND its age are SELF-COMPUTED point-in-time from
data/master/historical_regimes.parquet (small file, module-cached,
column-pruned read). The file ends 2026-06-05: when the signal date is past
the file's last row the strategy degrades to [] (no stale-age guessing).

Weekly cadence, LONG-only, at most 10 names per firing.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import liquid_pool

__all__ = ['RegimeAgeMomentum']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_regime_age_momentum'

_ROOT = os.path.join(os.path.dirname(__file__), '..', '..', '..')
REGIMES_PARQUET = os.path.abspath(os.path.join(_ROOT, 'data', 'master', 'historical_regimes.parquet'))

_REGIMES_CACHE: Optional[pd.DataFrame] = None


def _load_regimes() -> pd.DataFrame:
    """historical_regimes.parquet -> DataFrame[date, regime], module-cached.

    Small file (~2.5k rows); column-pruned direct read is safe on the VPS.
    Returns an EMPTY frame on any failure (callers degrade to []).
    """
    global _REGIMES_CACHE
    if _REGIMES_CACHE is not None:
        return _REGIMES_CACHE
    if not os.path.isfile(REGIMES_PARQUET):
        _REGIMES_CACHE = pd.DataFrame()
        return _REGIMES_CACHE
    try:
        df = pd.read_parquet(REGIMES_PARQUET, columns=['date', 'regime'])
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.dropna(subset=['date', 'regime']).sort_values('date').reset_index(drop=True)
        _REGIMES_CACHE = df
    except Exception:
        _REGIMES_CACHE = pd.DataFrame()
    return _REGIMES_CACHE


class RegimeAgeMomentum(BaseStrategy):
    """Fresh regime -> 6m momentum longs; aged LOW_VOL -> 5d reversal longs."""

    id                = STRATEGY_ID
    name              = 'Regime-Age Momentum'
    description       = ('Self-computed regime age from historical_regimes.parquet: fresh regime '
                         '(age <= 10d) -> LONG top-decile 6m momentum; LOW_VOL aged > 60d -> LONG '
                         'bottom-decile 5d reversal picks. Weekly, max 10.')
    tier              = 3
    signal_frequency  = 'weekly'
    min_lookback      = 140
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 10

    FRESH_AGE_MAX   = 10
    AGED_LOWVOL_MIN = 60
    MOM_DAYS        = 126     # ~6 months
    REV_DAYS        = 5
    PICK_COUNT      = 10
    BASE_SIZE_LONG  = 0.015

    def default_parameters(self) -> dict:
        return {
            'fresh_age_max':   self.FRESH_AGE_MAX,
            'aged_lowvol_min': self.AGED_LOWVOL_MIN,
            'mom_days':        self.MOM_DAYS,
            'rev_days':        self.REV_DAYS,
            'pick_count':      self.PICK_COUNT,
            'pool_size':       500,
        }

    @staticmethod
    def _week_boundary(index: pd.DatetimeIndex) -> bool:
        """True on the first equity trading day of a new ISO week."""
        if len(index) < 2:
            return False
        d1 = pd.Timestamp(index[-1]).isocalendar()
        d0 = pd.Timestamp(index[-2]).isocalendar()
        return (d1.year, d1.week) != (d0.year, d0.week)

    @staticmethod
    def _regime_state_and_age(asof: pd.Timestamp):
        """Point-in-time (state, age_in_rows) from the regimes file, or None.

        None when the file is missing/empty, asof predates the file, or asof
        is PAST the file's last row (file ends 2026-06-05 — no stale ages).
        """
        df = _load_regimes()
        if df.empty:
            return None
        asof = pd.Timestamp(asof).normalize()
        if asof > df['date'].iloc[-1] or asof < df['date'].iloc[0]:
            return None
        cut = df.loc[df['date'] <= asof, 'regime'].to_numpy()
        if len(cut) == 0:
            return None
        last = cut[-1]
        flipped = (cut != last)[::-1]
        age = int(np.argmax(flipped)) if flipped.any() else len(cut)
        return str(last), age

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

        if not (self._week_boundary(eq.index) or self.cadence_reset(regime)):
            print('[debug] signals=0', file=sys.stderr)
            return []

        sa = self._regime_state_and_age(eq.index[-1])
        if sa is None:
            print('[debug] signals=0', file=sys.stderr)
            return []
        file_state, age = sa

        fresh_max = int(self.parameters.get('fresh_age_max', self.FRESH_AGE_MAX))
        aged_min  = int(self.parameters.get('aged_lowvol_min', self.AGED_LOWVOL_MIN))
        mom_days  = int(self.parameters.get('mom_days', self.MOM_DAYS))
        rev_days  = int(self.parameters.get('rev_days', self.REV_DAYS))
        picks     = int(self.parameters.get('pick_count', self.PICK_COUNT))

        if age <= fresh_max:
            mode = 'fresh_regime_momentum'
        elif file_state == 'LOW_VOL' and age > aged_min:
            mode = 'aged_lowvol_reversal'
        else:
            print('[debug] signals=0', file=sys.stderr)
            return []

        pool = liquid_pool(eq, max_names=int(self.parameters.get('pool_size', 500)),
                           lookback=60)
        pool = [t for t in pool if t in universe] or pool
        pool = [t for t in pool if t in eq.columns]
        if len(pool) < 30:
            print('[debug] signals=0', file=sys.stderr)
            return []

        px = eq[pool].astype('float64')
        if mode == 'fresh_regime_momentum':
            if len(px) < mom_days + 1:
                print('[debug] signals=0', file=sys.stderr)
                return []
            score = px.iloc[-1] / px.iloc[-(mom_days + 1)] - 1.0
            score = score.replace([np.inf, -np.inf], np.nan).dropna()
            if len(score) < 30:
                print('[debug] signals=0', file=sys.stderr)
                return []
            rank_pct = score.rank(pct=True)
            selected = rank_pct[rank_pct >= 0.90].sort_values(ascending=False).head(picks)
        else:
            ret5 = px.iloc[-1] / px.iloc[-(rev_days + 1)] - 1.0
            ret5 = ret5.replace([np.inf, -np.inf], np.nan).dropna()
            if len(ret5) < 30:
                print('[debug] signals=0', file=sys.stderr)
                return []
            rank_pct = ret5.rank(pct=True)
            # LONG the worst 5d performers (bottom decile) — calm-regime chop
            # mean-reverts. Rank flipped so higher = more extreme loser.
            selected = (1.0 - rank_pct)[rank_pct <= 0.10].sort_values(ascending=False).head(picks)

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]

        def _conf(ext: float) -> str:
            if ext >= 0.97:
                return 'HIGH'
            if ext >= 0.93:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []
        for ticker, ext in selected.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                continue
            price = float(raw)
            series = eq[ticker].dropna()
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
                position_size_pct=round(self.BASE_SIZE_LONG * scale, 6),
                confidence=_conf(float(ext)),
                signal_params={
                    'mode':         mode,
                    'regime_file':  file_state,
                    'regime_age':   age,
                    'rank_extremity': round(float(ext), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
