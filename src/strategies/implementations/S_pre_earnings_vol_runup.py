"""
Pre-Earnings Vol Run-up — long vol into the pre-earnings IV ramp.

Source: Dubinsky & Johannes (2006) and the pre-earnings implied-volatility
run-up literature: single-name IV rises predictably into scheduled earnings
as the anticipated jump gets priced. Buying vol EARLY in the ramp — while
IV is still cheap versus its own history — harvests the run-up. This is the
entry-side mirror of S_HV17's post-event straddle fade.

Signal: earnings_dte in [5, 12] (the ramp window, before the final-week
premium is fully priced) AND iv_rank < 40 (vol still cheap) -> BUY_VOL.
One shot per name per earnings cycle (45-day per-name cooldown — the
[5,12] window spans <= 7 calendar days, the next cycle is ~90 days out).
<= 8 signals/day, lowest iv_rank first.

Data: aux_data['options'] enriched panel (STALE — covers 2024-04-22 ->
2026-04-22 and the backtest loader silently serves the last row afterwards;
earnings_dte additionally needs earnings.parquet coverage, 2025-03 ->).
Returns [] gracefully whenever the options aux is absent or empty.

Engine note: BUY_VOL is simulated +1 (long delta-1) by unified_backtest,
so brackets are LONG-shaped (stop below entry, targets above).
"""
from __future__ import annotations

import sys
from typing import Dict, List

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['PreEarningsVolRunup']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_pre_earnings_vol_runup'


class PreEarningsVolRunup(BaseStrategy):
    """BUY_VOL names 5-12 days before earnings while iv_rank is still low."""

    id                = STRATEGY_ID
    name              = 'Pre-Earnings Vol Run-up'
    description       = ('BUY_VOL when earnings_dte in [5,12] and iv_rank < 40 — harvest the pre-earnings '
                         'IV ramp while vol is still cheap (Dubinsky-Johannes). One shot per name per '
                         'earnings cycle, <=8/day.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 20
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 8

    DTE_MIN             = 5
    DTE_MAX             = 12
    IV_RANK_MAX         = 40.0
    CYCLE_COOLDOWN_DAYS = 45     # one shot per earnings cycle (~90d apart)
    MIN_PRICE           = 5.0
    BASE_SIZE           = 0.015

    def __init__(self, parameters: dict = None):
        super().__init__(parameters)
        # Per-name earnings-cycle dedupe. One instance is created per backtest
        # and bars are processed chronologically, so this never sees the future.
        self._last_fire: Dict[str, pd.Timestamp] = {}

    def default_parameters(self) -> dict:
        return {
            'dte_min':             self.DTE_MIN,
            'dte_max':             self.DTE_MAX,
            'iv_rank_max':         self.IV_RANK_MAX,
            'cycle_cooldown_days': self.CYCLE_COOLDOWN_DAYS,
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

        opts = (aux_data or {}).get('options') or {}
        if not opts:
            # Graceful when the enriched options panel is absent (validator,
            # out-of-coverage backtest bars, live aux failures).
            print('[debug] signals=0', file=sys.stderr)
            return []

        dte_min = int(self.parameters.get('dte_min', self.DTE_MIN))
        dte_max = int(self.parameters.get('dte_max', self.DTE_MAX))
        ivr_max = float(self.parameters.get('iv_rank_max', self.IV_RANK_MAX))
        cd_days = int(self.parameters.get('cycle_cooldown_days', self.CYCLE_COOLDOWN_DAYS))

        asof = pd.Timestamp(eq.index[-1])
        uni = set(universe) if universe else None

        candidates = []   # (iv_rank, ticker, meta)
        for ticker, o in opts.items():
            if not isinstance(o, dict) or not isinstance(ticker, str):
                continue
            if ticker not in eq.columns:
                continue
            if uni is not None and ticker not in uni:
                continue
            dte = o.get('earnings_dte')
            ivr = o.get('iv_rank')
            if dte is None or ivr is None:
                continue
            try:
                dte = int(dte); ivr = float(ivr)
            except (TypeError, ValueError):
                continue
            if not (dte_min <= dte <= dte_max) or not np.isfinite(ivr) or ivr >= ivr_max:
                continue
            lf = self._last_fire.get(ticker)
            if lf is not None and (asof - lf).days < cd_days:
                continue                          # one shot per earnings cycle
            series = eq[ticker].dropna()
            if series.empty:
                continue
            price = float(series.iloc[-1])
            if not np.isfinite(price) or price < self.MIN_PRICE:
                continue
            candidates.append((ivr, ticker, {'dte': dte, 'price': price,
                                             'series': series, 'iv30': o.get('iv30')}))

        candidates.sort(key=lambda c: (c[0], c[1]))   # cheapest vol first, deterministic

        scale = self.position_scale(regime_state)
        signals: List[Signal] = []
        for ivr, ticker, m in candidates[:self.MAX_SIGNALS]:
            # BUY_VOL simulates long delta-1 -> LONG-shaped bracket.
            stops = self.compute_stops_and_targets(m['series'], 'LONG', m['price'],
                                                   regime_state=regime_state)
            conf = 'HIGH' if ivr < 20.0 else ('MED' if ivr < 30.0 else 'LOW')
            self._last_fire[ticker] = asof
            iv30 = m.get('iv30')
            signals.append(Signal(
                ticker=ticker,
                direction='BUY_VOL',
                entry_price=m['price'],
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(self.BASE_SIZE * scale, 6),
                confidence=conf,
                signal_params={
                    'earnings_dte': m['dte'],
                    'iv_rank':      round(float(ivr), 2),
                    'iv30':         (round(float(iv30), 4)
                                     if iv30 is not None and np.isfinite(float(iv30)) else None),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
