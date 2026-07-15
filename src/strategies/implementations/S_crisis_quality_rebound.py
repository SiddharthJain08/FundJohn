"""
Crisis Quality Rebound — buy beaten-down quality names as panic vol fades.

Active ONLY in HIGH_VOL / CRISIS regimes (diversifies the fund's thinnest
regime coverage). Hypothesis: in a stressed tape, high-quality balance
sheets (ROE > 15%, debt/equity < 1) that have been sold down >= 25% from
their 252d high are liquidity casualties, not credit risks; the moment
their own short-horizon realized vol starts DECLINING, forced selling is
exhausting and they rebound first.

Entry (daily, LONG-only, max 10/day):
  - quality: aux_data['financials'] roe > 0.15 (fraction units) AND
    debt_equity_ratio < 1.0
  - drawdown: close <= 0.75 x trailing 252d high
  - vol fade: trailing 5d realized vol < the 5d realized vol 5 bars ago
  - 10d cooldown per name, computed deterministically from the price panel:
    fire only on the first bar the price-side condition turns true within
    the trailing 10 bars (no hidden state).

financials.parquet coverage starts 2025 — bars without aux financials
degrade to [] (graceful).
"""
from __future__ import annotations

import sys
from typing import List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

try:
    from strategies.implementations._extra_panels import liquid_pool
except ImportError:  # direct-file import fallback (validate harness)
    from _extra_panels import liquid_pool

__all__ = ['CrisisQualityRebound']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_crisis_quality_rebound'


class CrisisQualityRebound(BaseStrategy):
    """HIGH_VOL/CRISIS only: LONG quality names 25%+ off highs with fading vol."""

    id                = STRATEGY_ID
    name              = 'Crisis Quality Rebound'
    description       = ('HIGH_VOL/CRISIS only: LONG quality names (roe > 15%, D/E < 1) trading '
                         '>= 25% below their 252d high whose 5d realized vol is declining; '
                         'max 10/day, 10d per-name cooldown.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 260
    active_in_regimes = ['HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 10

    ROE_MIN         = 0.15    # financials.parquet roe is a FRACTION (0.15 = 15%)
    DE_MAX          = 1.0
    DD_THRESHOLD    = 0.25    # >= 25% below 252d high
    HIGH_DAYS       = 252
    VOL_DAYS        = 5
    COOLDOWN_DAYS   = 10
    PICK_COUNT      = 10
    BASE_SIZE_LONG  = 0.015

    def default_parameters(self) -> dict:
        return {
            'roe_min':       self.ROE_MIN,
            'de_max':        self.DE_MAX,
            'dd_threshold':  self.DD_THRESHOLD,
            'cooldown_days': self.COOLDOWN_DAYS,
            'pick_count':    self.PICK_COUNT,
            'pool_size':     500,
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
        if not self.should_run(regime_state):        # HIGH_VOL / CRISIS only
            print('[debug] signals=0', file=sys.stderr)
            return []

        fins = (aux_data or {}).get('financials') or {}
        if not isinstance(fins, dict) or not fins:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Bound per-bar work: only ~272 trailing equity rows are consumed below,
        # so pre-slice a raw tail before the full-panel mask/copy. Union
        # calendar adds <= 2 weekend rows per 5 weekdays, so 620 raw rows always
        # contain >= min_lookback equity rows whenever the full panel would.
        if len(prices) > 620:
            prices = prices.iloc[-620:]
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

        pool = liquid_pool(eq, max_names=int(self.parameters.get('pool_size', 500)),
                           lookback=60)
        pool = [t for t in pool if t in universe] or pool

        roe_min = float(self.parameters.get('roe_min', self.ROE_MIN))
        de_max  = float(self.parameters.get('de_max', self.DE_MAX))

        def _quality(t: str) -> bool:
            f = fins.get(t)
            if not isinstance(f, dict):
                return False
            roe = f.get('roe')
            de  = f.get('debt_equity_ratio')
            try:
                return (roe is not None and de is not None
                        and np.isfinite(float(roe)) and np.isfinite(float(de))
                        and float(roe) > roe_min and float(de) < de_max)
            except (TypeError, ValueError):
                return False

        quality = [t for t in pool if t in eq.columns and _quality(t)]
        if not quality:
            print('[debug] signals=0', file=sys.stderr)
            return []

        dd_thr   = float(self.parameters.get('dd_threshold', self.DD_THRESHOLD))
        cooldown = int(self.parameters.get('cooldown_days', self.COOLDOWN_DAYS))
        picks    = int(self.parameters.get('pick_count', self.PICK_COUNT))

        # Trailing window big enough for a 252d high on every cooldown bar.
        tail = eq[quality].astype('float64').iloc[-(self.HIGH_DAYS + cooldown + 10):]
        roll_hi = tail.rolling(self.HIGH_DAYS, min_periods=200).max()
        dd = tail / roll_hi - 1.0                                  # <= 0
        rets = tail.pct_change()
        v5 = rets.rolling(self.VOL_DAYS).std()
        cond = (dd <= -dd_thr) & (v5 < v5.shift(self.VOL_DAYS))

        last = cond.iloc[-1].fillna(False)
        prior = cond.iloc[-(cooldown + 1):-1].fillna(False).any()  # 10d cooldown
        fire = last & ~prior
        candidates = [t for t in quality if bool(fire.get(t, False))]
        if not candidates:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Deepest drawdown first (largest rebound potential), name asc ties.
        dd_today = dd.iloc[-1]
        candidates = sorted(candidates, key=lambda t: (float(dd_today[t]), t))[:picks]

        scale   = self.position_scale(regime_state)
        current = eq.iloc[-1]

        signals: List[Signal] = []
        for ticker in candidates:
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw = current.get(ticker)
            if raw is None or not np.isfinite(raw) or raw <= 0:
                continue
            price = float(raw)
            d = float(dd_today[ticker])
            confidence = 'HIGH' if d <= -0.40 else ('MED' if d <= -0.32 else 'LOW')
            series = eq[ticker].dropna()
            stops = self.compute_stops_and_targets(series, 'LONG', price,
                                                   regime_state=regime_state)
            f = fins.get(ticker, {})
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(self.BASE_SIZE_LONG * scale, 6),
                confidence=confidence,
                signal_params={
                    'drawdown_252d': round(d, 4),
                    'roe':           round(float(f.get('roe')), 4) if f.get('roe') is not None else None,
                    'debt_equity':   round(float(f.get('debt_equity_ratio')), 4) if f.get('debt_equity_ratio') is not None else None,
                    'vol5_now':      round(float(v5.iloc[-1][ticker]), 5) if pd.notna(v5.iloc[-1][ticker]) else None,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
