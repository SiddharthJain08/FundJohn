"""
12-1 Month Cross-Sectional Price Momentum
Ranks universe by 12-month return skipping the most recent month,
goes long the top decile. Classic Jegadeesh-Titman (1993) signal.
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['Momentum12_1']


class Momentum12_1(BaseStrategy):
    id               = 'momentum_12_1'
    name             = '12-1 Month Cross-Sectional Momentum'
    description      = '12m momentum (skip 1m) long top-10 ranked names'
    tier             = 2
    signal_frequency = 'daily'
    min_lookback     = 273          # 252 lookback + 21 skip
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'lookback_long':  252,   # ~12 months of trading days
            'lookback_skip':   21,   # skip last month (reversal avoidance)
            'top_n':           10,   # long positions
            'min_mom_thresh': 0.03,  # minimum 12mo return to qualify
        }

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
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

        p        = self.parameters
        lookback = p.get('lookback_long', 252)
        skip     = p.get('lookback_skip', 21)
        top_n    = p.get('top_n', 10)
        min_mom  = p.get('min_mom_thresh', 0.03)
        need     = lookback + skip

        if len(prices) < need:
            print('[debug] signals=0', file=sys.stderr)
            return []

        available = [
            t for t in universe
            if t in prices.columns
            and not t.startswith('^')
            and not t.endswith('=F')
            and '-USD' not in t
        ]
        if len(available) < 5:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Compute momentum per ticker on its own non-NaN trading days
        mom: dict = {}
        for t in available:
            s = prices[t].dropna()
            if len(s) < need:
                continue
            try:
                m = float(s.iloc[-skip]) / float(s.iloc[-need]) - 1
            except (ZeroDivisionError, ValueError, IndexError):
                continue
            if m >= min_mom:
                mom[t] = m

        mom_series = pd.Series(mom)
        if len(mom_series) < 3:
            print('[debug] signals=0', file=sys.stderr)
            return []

        top_names = mom_series.nlargest(top_n).index.tolist()
        scale     = self.position_scale(regime_state)
        pos_size  = round((1.0 / len(top_names)) * 0.18 * scale, 4)

        signals: List[Signal] = []
        for ticker in top_names:
            ts = prices[ticker].dropna()
            if len(ts) < 14:
                continue

            current = float(ts.iloc[-1])
            stops   = self.compute_stops_and_targets(
                ts, 'LONG', current, regime_state=regime_state
            )
            rank_pct = float(mom_series.rank(pct=True).get(ticker, 0.5))

            if rank_pct >= 0.90:
                confidence = 'HIGH'
            elif rank_pct >= 0.70:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'momentum_12_1': round(float(mom_series.get(ticker, 0.0)), 4),
                    'momentum_rank': round(rank_pct, 4),
                    'lookback_days': lookback,
                    'skip_days':     skip,
                    'regime':        regime_state,
                    'scale':         scale,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
