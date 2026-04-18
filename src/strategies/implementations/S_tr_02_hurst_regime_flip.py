from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['HurstRegimeFlip']


def _hurst_rs(arr: np.ndarray) -> float:
    """
    Rescaled range (R/S) Hurst exponent estimate (Mandelbrot & Wallis 1969, Lo 1991).
    Requires at least 50 observations. Returns NaN on failure.
    """
    n = len(arr)
    if n < 50:
        return np.nan

    lags = np.unique(np.floor(np.geomspace(10, n // 2, 18)).astype(int))
    log_lags, log_rs = [], []

    for lag in lags:
        blocks = n // lag
        if blocks < 2:
            continue
        sub = arr[: blocks * lag].reshape(blocks, lag)
        means  = sub.mean(axis=1, keepdims=True)
        dev    = (sub - means).cumsum(axis=1)
        r      = dev.max(axis=1) - dev.min(axis=1)
        s      = sub.std(axis=1, ddof=1)
        valid  = s > 0
        if valid.sum() == 0:
            continue
        rs_mean = (r[valid] / s[valid]).mean()
        if rs_mean > 0:
            log_lags.append(np.log(lag))
            log_rs.append(np.log(rs_mean))

    if len(log_lags) < 4:
        return np.nan

    H = float(np.polyfit(log_lags, log_rs, 1)[0])
    return float(np.clip(H, 0.0, 1.0))


class HurstRegimeFlip(BaseStrategy):
    """
    TRANSITIONING-regime Hurst classifier.
    LONG tickers with H > 0.53 (trending), SHORT tickers with H < 0.47 (mean-reverting).
    Deadband [0.47, 0.53] = no trade. Per Mandelbrot & Wallis (1969), Lo (1991).
    """

    id               = 'S_tr_02_hurst_regime_flip'
    name             = 'HurstRegimeFlip'
    description      = 'Hurst R/S regime flip — LONG trending names, SHORT mean-reverting names in TRANSITIONING regime'
    tier             = 1
    active_in_regimes = ['TRANSITIONING']
    min_lookback      = 252

    H_MOMENTUM_THRESH = 0.53
    H_MEAN_REV_THRESH = 0.47
    RS_LOOKBACK       = 252
    BASE_SIZE_PCT     = 0.015
    VOL_WINDOW        = 21

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

        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            return []

        price_data = prices[tickers].ffill()
        if len(price_data) < self.min_lookback:
            print(f'[debug] {self.id}: signals=0 (need {self.min_lookback} rows, got {len(price_data)})',
                  file=sys.stderr)
            return []

        returns = price_data.pct_change().dropna(how='all')
        latest  = price_data.iloc[-1]
        vol     = returns.iloc[-self.VOL_WINDOW:].std() * np.sqrt(252)

        momentum_cands: list = []  # (ticker, H)
        mean_rev_cands: list = []

        for ticker in tickers:
            series = returns[ticker].dropna().values
            if len(series) < 50:
                continue
            arr = series[-self.RS_LOOKBACK:]
            H   = _hurst_rs(arr)
            if np.isnan(H):
                continue
            if H > self.H_MOMENTUM_THRESH:
                momentum_cands.append((ticker, H))
            elif H < self.H_MEAN_REV_THRESH:
                mean_rev_cands.append((ticker, H))

        if not momentum_cands and not mean_rev_cands:
            print(f'[debug] {self.id}: signals=0 (no tickers passed H gate)', file=sys.stderr)
            return []

        # Sort by strongest signal
        momentum_cands.sort(key=lambda x: x[1], reverse=True)
        mean_rev_cands.sort(key=lambda x: x[1])

        scale        = self.position_scale(regime_state)
        signals: List[Signal] = []
        max_per_side = self.MAX_SIGNALS // 2

        for direction, candidates in [
            ('LONG',  momentum_cands[:max_per_side]),
            ('SHORT', mean_rev_cands[:max_per_side]),
        ]:
            for ticker, H in candidates:
                price = float(latest.get(ticker, 0))
                if price <= 0:
                    continue
                ticker_vol = max(float(vol.get(ticker, 0.20)), 1e-4)
                size = float(self.BASE_SIZE_PCT * (0.15 / ticker_vol) * scale)
                size = max(0.001, min(size, 0.05))

                deviation  = abs(H - 0.50)
                confidence = 'HIGH' if deviation > 0.08 else ('MED' if deviation > 0.05 else 'LOW')

                st = self.compute_stops_and_targets(
                    price_data[ticker].dropna(), direction, price,
                    regime_state=regime_state,
                )
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = direction,
                    entry_price       = round(price, 4),
                    stop_loss         = st['stop'],
                    target_1          = st['t1'],
                    target_2          = st['t2'],
                    target_3          = st['t3'],
                    position_size_pct = size,
                    confidence        = confidence,
                    signal_params     = {
                        'hurst':      round(H, 4),
                        'deviation':  round(deviation, 4),
                        'vol_annual': round(ticker_vol, 4),
                    },
                ))

        print(f'[debug] {self.id}: signals={len(signals)} '
              f'(momentum={len(momentum_cands)}, mean_rev={len(mean_rev_cands)})', file=sys.stderr)
        return signals
