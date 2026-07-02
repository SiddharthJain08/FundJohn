"""
Sector Momentum Rotational System — Quantpedia Strategy #62 clean-room port.

Universe: 10 US sector ETFs (VNQ, XLK, XLE, XLV, XLF, XLI, XLB, XLY, XLP, XLU).
Monthly rebalance: rank all 10 by 252-trading-day ROC; go long the top 3 equally
weighted (1/3 each). Positions not in the top 3 are removed at each rebalance.

Source: https://quantpedia.com/strategies/sector-momentum-rotational-system/
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['SectorMomentumRotationalSystem']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_ast_sector_momentum_rotational_system'

# Fixed universe — 10 US sector ETFs
BASKET  = ('VNQ', 'XLK', 'XLE', 'XLV', 'XLF', 'XLI', 'XLB', 'XLY', 'XLP', 'XLU')
LOOKBACK = 252   # 12-month ROC in trading days
TOP_N    = 3     # hold top 3 by momentum


class SectorMomentumRotationalSystem(BaseStrategy):
    """Monthly cross-sectional momentum rotator across 10 US sector ETFs.

    Source: https://quantpedia.com/strategies/sector-momentum-rotational-system/
    """

    id                = STRATEGY_ID
    name              = 'Sector Momentum Rotational System'
    description       = (
        'Sector ETFs with the strongest 12-month trailing momentum outperform '
        'over the next month; rotating monthly into the top 3 sectors captures '
        'the cross-sectional momentum premium.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = TOP_N

    def default_parameters(self) -> dict:
        return {
            'lookback':      LOOKBACK,
            'top_n':         TOP_N,
            'pos_size_frac': round(1.0 / TOP_N, 6),  # ~0.333 each
        }

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

        # Monthly trigger: fire only on the first trading day of a new month.
        if len(prices) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        idx = prices.index
        if not hasattr(idx[-1], 'month') or not hasattr(idx[-2], 'month'):
            print('[debug] signals=0', file=sys.stderr)
            return []
        if idx[-1].month == idx[-2].month:
            print('[debug] signals=0', file=sys.stderr)
            return []

        lookback = int(self.parameters.get('lookback', LOOKBACK))
        top_n    = int(self.parameters.get('top_n', TOP_N))

        if len(prices) < lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Identify basket members present in the price panel.
        available = [t for t in BASKET if t in prices.columns]
        if not available:
            print('[SectorMomentumRotationalSystem] no BASKET tickers in prices', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Compute 252-day ROC for each available ticker.
        roc: dict[str, float] = {}
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < lookback:
                continue
            try:
                ret = float(series.iloc[-1]) / float(series.iloc[-lookback]) - 1.0
            except (ZeroDivisionError, ValueError):
                continue
            roc[ticker] = ret

        if not roc:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Rank by ROC descending, pick top N.
        ranked = sorted(roc.items(), key=lambda x: x[1], reverse=True)
        top    = ranked[:top_n]

        scale         = self.position_scale(regime_state)
        pos_size_frac = float(self.parameters.get('pos_size_frac', round(1.0 / TOP_N, 6)))

        signals = []
        for rank_idx, (ticker, mom) in enumerate(top):
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(
                series,
                direction='LONG',
                current_price=current_price,
                regime_state=regime_state,
            )

            if mom > 0.15:
                confidence = 'HIGH'
            elif mom > 0.05:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(pos_size_frac * scale, 4),
                confidence        = confidence,
                signal_params     = {
                    'roc_252d': round(mom, 4),
                    'regime':   regime_state,
                    'scale':    scale,
                    'rank':     rank_idx + 1,
                    'rebalance': True,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
