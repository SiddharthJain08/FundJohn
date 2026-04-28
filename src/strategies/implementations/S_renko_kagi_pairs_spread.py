from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List, Optional, Tuple
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['RenkoKagiPairsSpread']


class RenkoKagiPairsSpread(BaseStrategy):
    """Pairs trading via renko/kagi chart reversals on vol-calibrated spread bricks."""

    id                 = 'S_renko_kagi_pairs_spread'
    name               = 'RenkoKagiPairsSpread'
    description        = 'Pairs trading via renko/kagi chart reversals on vol-calibrated spread bricks'
    tier               = 2
    min_lookback       = 504
    active_in_regimes  = ['LOW_VOL', 'TRANSITIONING']

    CORR_LOOKBACK   = 252
    BRICK_LOOKBACK  = 60
    MIN_CORR        = 0.72
    MAX_PAIRS       = 12
    POS_PER_LEG     = 0.025   # fraction of portfolio per leg before regime scale

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        # Keep tickers with ≥80% non-null coverage
        avail = [t for t in universe if t in prices.columns]
        if len(avail) < 4:
            print(f'[debug] signals=0', file=sys.stderr)
            return []
        pdata = prices[avail].dropna(axis=1, thresh=int(len(prices) * 0.80)).ffill()
        if len(pdata) < self.min_lookback:
            print(f'[debug] signals=0', file=sys.stderr)
            return []
        tickers = list(pdata.columns)

        # Correlation screen
        rets  = pdata.pct_change().dropna().tail(self.CORR_LOOKBACK)
        corr  = rets.corr()
        pairs: list[tuple] = []
        for i in range(len(tickers)):
            for j in range(i + 1, len(tickers)):
                c = float(corr.iloc[i, j])
                if c >= self.MIN_CORR:
                    pairs.append((tickers[i], tickers[j], c))
        pairs.sort(key=lambda x: -x[2])
        pairs = pairs[:self.MAX_PAIRS]

        if not pairs:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        signals: List[Signal] = []
        for ticker_a, ticker_b, _ in pairs:
            sig = self._signals_for_pair(pdata, ticker_a, ticker_b, regime_state, scale)
            signals.extend(sig)
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]

    # ------------------------------------------------------------------ #
    def _signals_for_pair(
        self,
        pdata: pd.DataFrame,
        ticker_a: str,
        ticker_b: str,
        regime_state: str,
        scale: float,
    ) -> List[Signal]:
        sa = pdata[ticker_a]
        sb = pdata[ticker_b]

        # OLS hedge ratio on trailing BRICK_LOOKBACK window
        ya = sa.iloc[-(self.BRICK_LOOKBACK + 1):].values.astype(float)
        xb = sb.iloc[-(self.BRICK_LOOKBACK + 1):].values.astype(float)
        cov = np.cov(ya, xb)
        hedge = float(cov[0, 1] / cov[1, 1]) if cov[1, 1] > 1e-12 else 1.0

        spread = sa - hedge * sb

        # Brick size = mean |daily change| over lookback window
        brick = float(spread.diff().abs().tail(self.BRICK_LOOKBACK).mean())
        if brick <= 0:
            return []

        reversal, direction = self._renko_reversal(spread.values, brick)
        if not reversal or direction == 0:
            return []

        pa = float(sa.iloc[-1])
        pb = float(sb.iloc[-1])
        pos = min(self.POS_PER_LEG * scale, 0.08)
        conf = 'MED'

        if direction == 1:   # spread reversing UP → LONG A / SHORT B
            dir_a, dir_b = 'LONG', 'SHORT'
        else:                # spread reversing DOWN → SHORT A / LONG B
            dir_a, dir_b = 'SHORT', 'LONG'

        st_a = self.compute_stops_and_targets(sa, dir_a, pa, regime_state=regime_state)
        st_b = self.compute_stops_and_targets(sb, dir_b, pb, regime_state=regime_state)
        spread_tag = 'UP' if direction == 1 else 'DOWN'

        return [
            Signal(ticker=ticker_a, direction=dir_a, entry_price=pa,
                   stop_loss=st_a['stop'], target_1=st_a['t1'],
                   target_2=st_a['t2'], target_3=st_a['t3'],
                   position_size_pct=pos, confidence=conf,
                   signal_params={'pair': ticker_b, 'hedge': round(hedge, 4),
                                  'spread_dir': spread_tag, 'chart': 'renko'}),
            Signal(ticker=ticker_b, direction=dir_b, entry_price=pb,
                   stop_loss=st_b['stop'], target_1=st_b['t1'],
                   target_2=st_b['t2'], target_3=st_b['t3'],
                   position_size_pct=pos, confidence=conf,
                   signal_params={'pair': ticker_a, 'hedge': round(1.0 / hedge if hedge != 0 else 1.0, 4),
                                  'spread_dir': spread_tag, 'chart': 'renko'}),
        ]

    # ------------------------------------------------------------------ #
    @staticmethod
    def _renko_reversal(
        spread_values: np.ndarray,
        brick_size: float,
    ) -> Tuple[bool, int]:
        """
        Build renko bricks on spread.
        Returns (reversal_today, direction) where direction ∈ {-1, 0, 1}.
        reversal_today=True iff the most recent observation caused a direction flip.
        """
        if len(spread_values) < 10:
            return False, 0

        ref       = float(spread_values[0])
        last_dir  = 0    # 0 = no brick yet
        prev_dir  = 0
        bricks: list[int] = []

        for price in spread_values[1:]:
            price = float(price)
            diff  = price - ref
            if abs(diff) >= brick_size:
                n     = max(1, int(abs(diff) / brick_size))
                d     = 1 if diff > 0 else -1
                ref  += d * brick_size * n
                bricks.extend([d] * n)
                prev_dir = last_dir
                last_dir = d

        if len(bricks) < 2:
            return False, 0

        # Reversal detected if last brick direction ≠ second-to-last brick direction
        reversal = (bricks[-1] != bricks[-2])
        return reversal, bricks[-1]
