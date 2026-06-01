"""S_sector_flow_confirmed_momentum — momentum corroborated by sector/broad-market alignment.

Two modes (param 'mode', overridable by env OPENCLAW_SECTOR_MODE), both long & short:
  - 'confirm' (default): per-stock momentum kept only when sector_flow.confirm() agrees
    (LONG needs aligned up-sector + up-market; SHORT needs aligned down-sector + down-market).
  - 'basket': rank sectors by SPDR-ETF momentum; LONG the constituents of the strongest
    top_sectors and SHORT the constituents of the weakest top_sectors (symmetric).

Lift test (confirm mode): OPENCLAW_CONFIRM_BASE_ONLY=1 drops the sector gate.
Trades the underlying equity. Zero LLM tokens.
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal
from strategies.confirmation import momentum_base as mb
from strategies.confirmation import sector_flow as sf
from strategies.confirmation import sector_map as sm

INSTRUMENT_CLASS = 'equity'


class SectorFlowConfirmedMomentum(BaseStrategy):
    id = 'S_sector_flow_confirmed_momentum'
    name = 'Sector-Flow Confirmed Momentum'
    description = 'Momentum corroborated by sector/broad-market ETF alignment; confirm + basket modes (LONG & SHORT)'
    tier = 2
    signal_frequency = 'daily'
    min_lookback = 70
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    data_requirements = ['prices']

    def default_parameters(self) -> dict:
        return {'mode': 'confirm', 'lookback': 63, 'skip': 5, 'decile': 0.10,
                'max_each': 20, 'top_sectors': 2, 'sector_lookback': 63}

    def _mk_signal(self, prices, t, direction, regime_state, scale, extra):
        ts = prices[t].dropna()
        if len(ts) < 2:
            return None
        cur = float(ts.iloc[-1])
        if cur <= 0:
            return None
        stops = self.compute_stops_and_targets(ts, direction, cur, regime_state=regime_state)
        return Signal(ticker=t, direction=direction, entry_price=cur,
                      stop_loss=stops['stop'], target_1=stops['t1'],
                      target_2=stops['t2'], target_3=stops['t3'],
                      position_size_pct=round((1.0 / max(self.parameters['max_each'], 1)) * scale, 4),
                      confidence='MED', signal_params=extra)

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        if len(prices) < self.min_lookback:
            return []
        p = self.parameters
        mode = os.environ.get('OPENCLAW_SECTOR_MODE', p['mode'])
        scale = self.position_scale(regime_state)
        as_of = prices.index[-1]
        signals: List[Signal] = []

        if mode == 'basket':
            etf_scores = mb.momentum_scores(prices, list(sm.SECTOR_ETF.values()),
                                            p['sector_lookback'], p['skip'])
            sector_by_etf = {v: k for k, v in sm.SECTOR_ETF.items()}
            ranked = sorted(etf_scores.items(), key=lambda kv: kv[1], reverse=True)
            top = [sector_by_etf[e] for e, _ in ranked[:p['top_sectors']]]
            bot = [sector_by_etf[e] for e, _ in ranked[-p['top_sectors']:]]
            for sector, direction in ([(s, 'LONG') for s in top] + [(s, 'SHORT') for s in bot]):
                for t in sm.constituents(sector):
                    if t in universe and t in prices.columns:
                        sig = self._mk_signal(prices, t, direction, regime_state, scale,
                                              {'mode': 'basket', 'sector': sector})
                        if sig:
                            signals.append(sig)
            return signals[:self.MAX_SIGNALS]

        # confirm mode
        base_only = os.environ.get('OPENCLAW_CONFIRM_BASE_ONLY') == '1'
        scores = mb.momentum_scores(prices, universe, p['lookback'], p['skip'])
        longs, shorts = mb.rank_long_short(scores, p['decile'], p['max_each'])
        for tickers, direction in ((longs, 'LONG'), (shorts, 'SHORT')):
            for t in tickers:
                if t not in prices.columns:
                    continue
                if not base_only:
                    passes, _ = sf.confirm(direction, t, prices, sm, as_of)
                    if not passes:
                        continue
                sig = self._mk_signal(prices, t, direction, regime_state, scale,
                                      {'mode': 'confirm', 'momentum': round(float(scores.get(t, 0.0)), 4),
                                       'base_only': base_only})
                if sig:
                    signals.append(sig)
        return signals[:self.MAX_SIGNALS]
