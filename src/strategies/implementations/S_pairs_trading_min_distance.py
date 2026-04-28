from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['PairsTradingMinDistance']


class PairsTradingMinDistance(BaseStrategy):
    """Minimum-distance pairs trading: long-short self-financing when normalized spread exceeds 2σ."""

    id          = 'S_pairs_trading_min_distance'
    name        = 'PairsTradingMinDistance'
    description = (
        'Gatev-Goetzmann-Rouwenhorst minimum-distance pairs: '
        'normalized cumulative-return spread entry at 2σ, exit at zero-crossing.'
    )
    tier             = 2
    min_lookback     = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    FORMATION_DAYS = 252
    TRADING_DAYS   = 126
    TOP_PAIRS      = 20
    ENTRY_Z        = 2.0
    BASE_SIZE      = 0.01   # 1% per leg

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

        # Filter to tickers present in prices with sufficient history
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 50:
            print(f'[debug] signals=0 (universe<50: {len(tickers)})', file=sys.stderr)
            return []

        lookback = self.FORMATION_DAYS + self.TRADING_DAYS
        px = prices[tickers].ffill().tail(lookback)

        # Drop tickers with too many NaNs
        px = px.dropna(axis=1, thresh=int(lookback * 0.9))
        tickers = list(px.columns)

        if len(tickers) < 20 or len(px) < lookback:
            print(f'[debug] signals=0 (insufficient data)', file=sys.stderr)
            return []

        # Formation window: normalize cumulative returns to start at 1
        form_px = px.iloc[:self.FORMATION_DAYS]
        norm_form = form_px / form_px.iloc[0]   # shape: (252, n_tickers)

        # Compute pairwise sum-of-squared-differences
        mat = norm_form.values.T   # (n, T)
        n = len(tickers)
        # Use vectorized approach: SSD(i,j) = sum((r_i - r_j)^2)
        # = sum(r_i^2) + sum(r_j^2) - 2*sum(r_i*r_j)
        sq_sum = (mat ** 2).sum(axis=1)           # (n,)
        dot    = mat @ mat.T                       # (n, n)
        ssd    = sq_sum[:, None] + sq_sum[None, :] - 2 * dot   # (n, n)

        # Zero out diagonal and lower triangle to get unique pairs
        ssd_upper = np.where(np.triu(np.ones((n, n), dtype=bool), k=1), ssd, np.inf)

        # Select top-N closest pairs (smallest SSD)
        flat_idx = np.argsort(ssd_upper.ravel())[:self.TOP_PAIRS * 3]
        pair_candidates = [(flat_idx[k] // n, flat_idx[k] % n)
                           for k in range(len(flat_idx))
                           if ssd_upper.ravel()[flat_idx[k]] < np.inf]

        # Formation-period sigma for each pair
        pair_sigma: dict = {}
        for (i_idx, j_idx) in pair_candidates[:self.TOP_PAIRS * 2]:
            spread_form = norm_form.iloc[:, i_idx].values - norm_form.iloc[:, j_idx].values
            sigma = float(spread_form.std())
            if sigma > 1e-8:
                pair_sigma[(i_idx, j_idx)] = sigma

        if not pair_sigma:
            print(f'[debug] signals=0 (no valid pairs)', file=sys.stderr)
            return []

        # Sort by SSD to keep closest pairs
        sorted_pairs = sorted(pair_sigma.keys(),
                              key=lambda p: float(ssd_upper[p[0], p[1]]))[:self.TOP_PAIRS]

        # Trading window: last TRADING_DAYS
        trade_px = px.iloc[self.FORMATION_DAYS:]
        norm_trade = trade_px / form_px.iloc[0]   # continue normalization from formation start

        signals: List[Signal] = []
        for (i_idx, j_idx) in sorted_pairs:
            ti = tickers[i_idx]
            tj = tickers[j_idx]
            sigma = pair_sigma[(i_idx, j_idx)]

            spread_trade = norm_trade.iloc[:, i_idx].values - norm_trade.iloc[:, j_idx].values
            if len(spread_trade) == 0:
                continue

            current_spread = float(spread_trade[-1])
            zscore = current_spread / sigma   # formation-period sigma as baseline

            if abs(zscore) < self.ENTRY_Z:
                continue

            # LONG the underperformer (negative spread), SHORT the outperformer
            if zscore > 0:
                # ti outperformed tj: short ti, long tj
                dir_i, dir_j = 'SHORT', 'LONG'
            else:
                # tj outperformed ti: long ti, short tj
                dir_i, dir_j = 'LONG', 'SHORT'

            conf  = 'HIGH' if abs(zscore) > 3.0 else ('MED' if abs(zscore) > 2.5 else 'LOW')
            size  = float(self.BASE_SIZE * scale)
            params = {
                'pair': f'{ti}_{tj}',
                'zscore': round(zscore, 3),
                'sigma': round(sigma, 5),
                'ssd': round(float(ssd_upper[i_idx, j_idx]), 5),
            }

            for ticker, direction in [(ti, dir_i), (tj, dir_j)]:
                price_series = prices[ticker].dropna()
                if price_series.empty:
                    continue
                current_price = float(price_series.iloc[-1])
                stops = self.compute_stops_and_targets(
                    price_series.tail(20), direction, current_price,
                    regime_state=regime_state,
                )
                signals.append(Signal(
                    ticker=ticker,
                    direction=direction,
                    entry_price=current_price,
                    stop_loss=float(stops['stop']),
                    target_1=float(stops['t1']),
                    target_2=float(stops['t2']),
                    target_3=float(stops['t3']),
                    position_size_pct=size,
                    confidence=conf,
                    signal_params=params,
                ))

            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
