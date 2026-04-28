"""
S_ma_tsmom_crossover — Moving Average Crossover vs TSMOM (Marshall, Nguyen & Visaltanachoti 2016)

MA crossover generates earlier trend entry/exit signals than TSMOM; vol-scaled sizing.
Active in LOW_VOL and TRANSITIONING regimes.
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['MATSMOMCrossover']


class MATSMOMCrossover(BaseStrategy):
    """MA crossover generates earlier trend signals than TSMOM; vol-scaled sizing (Marshall et al. 2016)."""

    id          = 'S_ma_tsmom_crossover'
    name        = 'MATSMOMCrossover'
    description = 'MA crossover generates earlier trend signals than TSMOM; vol-scaled sizing (Marshall et al. 2016).'
    tier        = 2
    min_lookback = 200
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    MA_WINDOWS = [10, 20, 50, 100, 200]
    MIN_BULLISH = 3          # need >= 3/5 MAs bullish
    TARGET_VOL  = 0.10       # 10% annualised vol per position
    MAX_WEIGHT  = 0.10       # hard cap per position

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        if aux_data is None:
            aux_data = {}

        # Filter to tickers that have enough price history
        max_window = max(self.MA_WINDOWS)
        valid = [t for t in universe if t in prices.columns]
        if not valid or len(prices) < max_window:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Score each ticker by number of MA windows where close > SMA(k)
        scores: dict[str, int] = {}
        for ticker in valid:
            series = prices[ticker].dropna()
            if len(series) < max_window:
                continue
            current_price = float(series.iloc[-1])
            if current_price <= 0:
                continue
            bullish_count = sum(
                1 for k in self.MA_WINDOWS
                if len(series) >= k and current_price > float(series.iloc[-k:].mean())
            )
            if bullish_count >= self.MIN_BULLISH:
                scores[ticker] = bullish_count

        if not scores:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Rank desc by score, cap at MAX_SIGNALS
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:self.MAX_SIGNALS]
        n = len(ranked)
        base_size = scale / max(n, 1)

        # Realised vol for vol-scaling (optional)
        rv_last: pd.Series | None = None
        rv_raw = aux_data.get('realized_vol')
        if rv_raw is not None and not (hasattr(rv_raw, 'empty') and rv_raw.empty):
            try:
                rv_last = rv_raw.iloc[-1]
            except Exception:
                rv_last = None

        signals: List[Signal] = []
        for ticker, bull_cnt in ranked:
            series = prices[ticker].dropna()
            current_price = float(series.iloc[-1])

            # Vol-scaled weight: target_vol / realized_vol, capped at MAX_WEIGHT
            weight = base_size
            if rv_last is not None and ticker in rv_last.index:
                rv = float(rv_last[ticker])
                if rv > 0:
                    weight = min(self.TARGET_VOL / rv * scale, base_size * 2.0)
            weight = float(min(weight, self.MAX_WEIGHT))

            stops = self.compute_stops_and_targets(
                series, 'LONG', current_price, regime_state=regime_state
            )

            if bull_cnt == len(self.MA_WINDOWS):
                confidence = 'HIGH'
            elif bull_cnt >= 4:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=float(current_price),
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']),
                target_2=float(stops['t2']),
                target_3=float(stops['t3']),
                position_size_pct=weight,
                confidence=confidence,
                signal_params={
                    'bullish_ma_count': bull_cnt,
                    'ma_windows': self.MA_WINDOWS,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
