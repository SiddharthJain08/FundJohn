"""
Return Asymmetry Effect in Commodity Futures (ETP Proxy)

Source: https://quantpedia.com/strategies/return-asymmetry-effect-in-commodity-futures/

At each month-start, compute the asymmetry index (IE) for each commodity ETP
using 260 daily returns. IE = count(days where return > mean+2*std) minus
count(days where return < mean-2*std). Long the bottom-7 IE names (least
right-skewed) and short the top-7 IE names (most right-skewed), equally
weighted at 1/7 per leg. Rebalance monthly.

Note: Original strategy uses futures continuous contracts (5x leverage).
This ETP proxy runs unlevered across the available commodity ETFs.
"""
from __future__ import annotations

import sys
import pandas as pd
import numpy as np
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['ReturnAsymmetryEffectInCommodityFutures']

INSTRUMENT_CLASS = 'etp'

# ETP proxies for the commodity futures universe (coverage in prices.parquet varies)
ETP_BASKET = [
    'GLD',  # Gold
    'SLV',  # Silver
    'GDX',  # Gold miners / metals proxy
    'USO',  # WTI Crude Oil
    'UNG',  # Natural Gas
    'PDBC', # Diversified commodities
    'CPER', # Copper
    'WEAT', # Wheat
    'CORN', # Corn
    'SOYB', # Soybeans
    'PALL', # Palladium
    'PPLT', # Platinum
    'CANE', # Sugar
    'JO',   # Coffee
    'NIB',  # Cocoa
    'BAL',  # Cotton
]

LOOKBACK = 261    # 261 prices → 260 daily returns
LONG_N = 7
SHORT_N = 7
MIN_INSTRUMENTS = 14


class ReturnAsymmetryEffectInCommodityFutures(BaseStrategy):
    """Monthly long bottom-7 / short top-7 commodity ETPs by return asymmetry (IE).

    IE = count(ret > mean+2σ) − count(ret < mean−2σ).
    Low IE → fewer extreme up-days relative to extreme down-days → go long.
    High IE → excess extreme up-days relative to down-days → go short.
    """

    id                = 'S_ast_return_asymmetry_effect_in_commodity_futures'
    name              = 'Return Asymmetry Effect in Commodity Futures (ETP Proxy)'
    description       = (
        'Monthly equal-weight long bottom-7 / short top-7 commodity ETPs '
        'ranked by return asymmetry index IE (extreme-up days minus extreme-down days).'
    )
    tier              = 3
    signal_frequency  = 'monthly'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {
            'lookback':        LOOKBACK,
            'long_n':          LONG_N,
            'short_n':         SHORT_N,
            'min_instruments': MIN_INSTRUMENTS,
            'pos_size_frac':   round(1.0 / LONG_N, 6),  # 1/7 each side
        }

    def _compute_ie(self, closes: pd.Series) -> float:
        """IE = count(ret > mu+2σ) - count(ret < mu-2σ) over the closes window."""
        rets = closes.pct_change().dropna()
        if len(rets) < 10:
            return float('nan')
        mu    = float(rets.mean())
        sigma = float(rets.std(ddof=1))
        thresh = 2.0 * sigma
        up   = int((rets > mu + thresh).sum())
        down = int((rets < mu - thresh).sum())
        return float(up - down)

    def _is_month_start(self, prices: pd.DataFrame) -> bool:
        """True if the last bar is the first trading day of a new calendar month."""
        if not isinstance(prices.index, pd.DatetimeIndex) or len(prices) < 2:
            return False
        return int(prices.index[-1].month) != int(prices.index[-2].month)

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Monthly rebalance only
        if not self._is_month_start(prices):
            print('[debug] signals=0', file=sys.stderr)
            return []

        lookback   = int(self.parameters.get('lookback', LOOKBACK))
        long_n     = int(self.parameters.get('long_n', LONG_N))
        short_n    = int(self.parameters.get('short_n', SHORT_N))
        min_instr  = int(self.parameters.get('min_instruments', MIN_INSTRUMENTS))
        pos_frac   = float(self.parameters.get('pos_size_frac', 1.0 / long_n))

        # Compute IE for all available ETPs with sufficient history
        ie_scores: dict[str, float] = {}
        for ticker in ETP_BASKET:
            if ticker not in prices.columns:
                continue
            series = prices[ticker].dropna()
            if len(series) < lookback:
                continue
            ie = self._compute_ie(series.iloc[-lookback:])
            if pd.notna(ie):
                ie_scores[ticker] = ie

        if len(ie_scores) < min_instr:
            print(f'[debug] signals=0 (only {len(ie_scores)} instruments, need {min_instr})',
                  file=sys.stderr)
            return []

        sorted_tickers = sorted(ie_scores, key=ie_scores.__getitem__)
        long_tickers   = sorted_tickers[:long_n]
        short_tickers  = sorted_tickers[-short_n:]

        scale    = self.position_scale(regime_state)
        pos_size = round(pos_frac * scale * 0.5, 4)  # 50% NAV each side max

        signals: List[Signal] = []

        for ticker in long_tickers:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            cur = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=cur, regime_state=regime_state
            )
            ie_val = ie_scores[ticker]
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = cur,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = 'HIGH' if ie_val <= -2 else 'MED',
                signal_params     = {
                    'ie_score': ie_val,
                    'ie_rank':  'long_bottom',
                    'regime':   regime_state,
                },
            ))

        for ticker in short_tickers:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            cur = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series, direction='SHORT', current_price=cur, regime_state=regime_state
            )
            ie_val = ie_scores[ticker]
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = cur,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = 'HIGH' if ie_val >= 2 else 'MED',
                signal_params     = {
                    'ie_score': ie_val,
                    'ie_rank':  'short_top',
                    'regime':   regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
