from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['SparseCCAMeanRevert']


class SparseCCAMeanRevert(BaseStrategy):
    """Sparse portfolios formed by maximizing mean reversion via sparse CCA; trade long/short on spread z-score."""

    id          = 'S_sparse_cca_mean_revert'
    name        = 'SparseCCAMeanRevert'
    description = 'Sparse portfolios formed by maximizing mean reversion via sparse CCA exhibit negative autocorrelation; trade long/short on spread z-score.'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    LOOKBACK    = 252
    LAG         = 5
    K_ASSETS    = 10
    Z_ENTRY     = 1.5
    Z_EXIT      = 0.25
    SIZE_PER    = 0.03   # 3% per leg asset × 10 = 30% max total

    def generate_signals(self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []
        scale = self.position_scale(regime_state)

        # --- data prep ---
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        min_rows = self.LOOKBACK + self.LAG + 20
        px = prices[tickers].dropna(axis=1, thresh=min_rows).tail(min_rows + 10)
        if px.shape[1] < 10 or len(px) < self.LOOKBACK + self.LAG:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rets = px.pct_change().dropna()
        if len(rets) < self.LOOKBACK:
            print('[debug] signals=0', file=sys.stderr)
            return []

        r = rets.tail(self.LOOKBACK)

        # --- sparse selection via per-asset lag autocorrelation (faithful approx of sparse CCA) ---
        autocorrs: dict[str, float] = {}
        for col in r.columns:
            s = r[col].dropna()
            if len(s) < 60:
                continue
            try:
                ac = float(s.autocorr(lag=self.LAG))
                if not np.isnan(ac):
                    autocorrs[col] = ac
            except Exception:
                continue

        if len(autocorrs) < 10:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Select K_ASSETS most negatively autocorrelated (most mean-reverting)
        sorted_tickers = sorted(autocorrs, key=lambda t: autocorrs[t])
        sparse_tickers = sorted_tickers[:self.K_ASSETS]

        # Rank-based weights: proportional to -autocorr, normalized by abs-sum
        raw = np.array([-autocorrs[t] for t in sparse_tickers])
        abs_sum = float(np.abs(raw).sum())
        if abs_sum < 1e-8:
            print('[debug] signals=0', file=sys.stderr)
            return []
        weights = {t: float(raw[i] / abs_sum) for i, t in enumerate(sparse_tickers)}

        # --- compute portfolio spread z-score ---
        px_sparse = px[sparse_tickers].copy()
        # Normalize each price to base 1 at start of window
        base = px_sparse.iloc[0].replace(0, np.nan)
        px_norm = px_sparse.div(base)
        w_series = pd.Series(weights)
        spread = px_norm.mul(w_series).sum(axis=1)

        if len(spread) < self.LOOKBACK:
            print('[debug] signals=0', file=sys.stderr)
            return []

        roll = spread.tail(self.LOOKBACK)
        roll_mean = float(roll.mean())
        roll_std  = float(roll.std())
        if roll_std < 1e-8:
            print('[debug] signals=0', file=sys.stderr)
            return []

        z = float((float(spread.iloc[-1]) - roll_mean) / roll_std)

        # --- threshold check ---
        if z < -self.Z_ENTRY:
            portfolio_dir = 'LONG'
        elif z > self.Z_ENTRY:
            portfolio_dir = 'SHORT'
        else:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        confidence = 'HIGH' if abs(z) > 2.0 else 'MED'
        today_px = px.iloc[-1]
        size = round(scale * self.SIZE_PER, 4)
        signals: List[Signal] = []

        for ticker in sparse_tickers:
            w = weights.get(ticker, 0.0)
            if abs(w) < 0.005:
                continue
            current_price = float(today_px.get(ticker, 0.0))
            if current_price <= 0:
                continue

            # LONG portfolio: buy positive-weight assets, sell negative-weight
            if portfolio_dir == 'LONG':
                direction = 'LONG' if w > 0 else 'SHORT'
            else:
                direction = 'SHORT' if w > 0 else 'LONG'

            stops = self.compute_stops_and_targets(
                px[ticker].dropna(), direction, current_price, regime_state=regime_state
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
                confidence=confidence,
                signal_params={
                    'z_score':  round(z, 3),
                    'autocorr': round(autocorrs.get(ticker, 0.0), 4),
                    'weight':   round(w, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]
