"""S_options_flow_confirmed_momentum — cross-sectional momentum corroborated by options flow.

Base: cross-sectional momentum (top/bottom decile, abs-momentum filtered).
Corroboration: keep a candidate only if options flow agrees — low PCR confirms LONG
(heavy call demand), high PCR confirms SHORT (heavy put demand); IV skew nudges the score.
Trades the underlying equity (instrument_class='equity'); never an option contract.

Lift test: set env OPENCLAW_CONFIRM_BASE_ONLY=1 to run the base momentum signal WITHOUT the
options gate, so backtest A/B isolates the corroboration's contribution.

Zero LLM tokens. Backtest window must be <= 2026-04-22 (options aux-loader forward-fills after).
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal
from strategies.confirmation import momentum_base as mb
from strategies.confirmation import options_flow as of

INSTRUMENT_CLASS = 'equity'


class OptionsFlowConfirmedMomentum(BaseStrategy):
    id = 'S_options_flow_confirmed_momentum'
    name = 'Options-Flow Confirmed Momentum'
    description = 'Cross-sectional momentum corroborated by put-call ratio / IV skew (LONG & SHORT)'
    tier = 2
    signal_frequency = 'daily'
    min_lookback = 70
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    data_requirements = ['prices', 'options_aggregates']

    def default_parameters(self) -> dict:
        return {'lookback': 63, 'skip': 5, 'decile': 0.10, 'max_each': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        if len(prices) < self.min_lookback:
            return []

        p = self.parameters
        base_only = os.environ.get('OPENCLAW_CONFIRM_BASE_ONLY') == '1'
        opts_map = (aux_data or {}).get('options', {})
        if not base_only and not opts_map:
            return []

        scores = mb.momentum_scores(prices, universe, p['lookback'], p['skip'])
        longs, shorts = mb.rank_long_short(scores, p['decile'], p['max_each'])
        scale = self.position_scale(regime_state)

        signals: List[Signal] = []
        for tickers, direction in ((longs, 'LONG'), (shorts, 'SHORT')):
            for t in tickers:
                if t not in prices.columns:
                    continue
                if not base_only:
                    passes, _score = of.confirm(direction, opts_map.get(t))
                    if not passes:
                        continue
                ts = prices[t].dropna()
                if len(ts) < 2:
                    continue
                cur = float(ts.iloc[-1])
                if cur <= 0:
                    continue
                stops = self.compute_stops_and_targets(ts, direction, cur, regime_state=regime_state)
                signals.append(Signal(
                    ticker=t, direction=direction, entry_price=cur,
                    stop_loss=stops['stop'], target_1=stops['t1'],
                    target_2=stops['t2'], target_3=stops['t3'],
                    position_size_pct=round((1.0 / max(p['max_each'], 1)) * scale, 4),
                    confidence='MED',
                    signal_params={'momentum': round(float(scores.get(t, 0.0)), 4),
                                   'pc_ratio': (opts_map.get(t) or {}).get('pc_ratio'),
                                   'base_only': base_only},
                ))
        return signals[:self.MAX_SIGNALS]
