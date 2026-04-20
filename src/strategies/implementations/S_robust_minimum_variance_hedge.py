from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['RobustMinimumVarianceHedge']


class RobustMinimumVarianceHedge(BaseStrategy):
    """Robust min-var hedge: rank by realized vol + uncertainty box → SHORT high-vol outliers."""

    id          = 'S_robust_minimum_variance_hedge'
    name        = 'RobustMinimumVarianceHedge'
    description = 'Robust min-var hedge: inv-vol rank + vol-excess uncertainty box → SHORT high-vol outliers'
    tier        = 2

    # Run in all regimes — spec has no regime filter
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    VOL_SHORT    = 21    # short-horizon RV window (days)
    VOL_LONG     = 63    # long-horizon RV for uncertainty box calibration
    TOP_PCT      = 0.10  # top 10% by RV = hedge candidates
    MAX_POS_PCT  = 0.05  # per-position cap (5% of portfolio)

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (empty prices)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime filtered)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)

        # Restrict to tickers present in both universe and prices
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 10:
            print(f'[debug] signals=0 (too few tickers={len(tickers)})', file=sys.stderr)
            return []

        if len(prices) < self.VOL_LONG + 5:
            print(f'[debug] signals=0 (insufficient history rows={len(prices)})', file=sys.stderr)
            return []

        price_slice = prices[tickers].ffill().dropna(axis=1, thresh=self.VOL_LONG)
        if price_slice.empty or price_slice.shape[1] < 5:
            print('[debug] signals=0 (empty after ffill/dropna)', file=sys.stderr)
            return []

        log_ret = np.log(price_slice / price_slice.shift(1))

        # Annualised realized vols
        rv_short = log_ret.rolling(self.VOL_SHORT).std() * np.sqrt(252)
        rv_long  = log_ret.rolling(self.VOL_LONG).std()  * np.sqrt(252)

        rv_now  = rv_short.iloc[-1].dropna()
        rvl_now = rv_long.iloc[-1].dropna()

        common = rv_now.index.intersection(rvl_now.index)
        if len(common) < 5:
            print('[debug] signals=0 (too few common tickers after vol calc)', file=sys.stderr)
            return []

        rv_now  = rv_now[common]
        rvl_now = rvl_now[common]

        # Uncertainty box proxy: vol increasing (short > long) means forecast harder
        vol_excess = rv_now - rvl_now   # positive → vol regime tightening, harder to forecast

        # Hedge candidates: top N by short-horizon RV
        n_top = max(3, int(len(common) * self.TOP_PCT))
        rv_ranked = rv_now.sort_values(ascending=False)
        candidates = rv_ranked.head(n_top)

        # Apply uncertainty box filter: only hedge where vol is rising
        candidates = candidates[vol_excess[candidates.index] > 0]

        if candidates.empty:
            print('[debug] signals=0 (no candidates after uncertainty box filter)', file=sys.stderr)
            return []

        # Inverse-vol weighting — closed-form MV approximation under equal correlations
        inv_vol     = 1.0 / candidates
        inv_vol_sum = inv_vol.sum()
        if inv_vol_sum == 0:
            print('[debug] signals=0 (inv_vol_sum=0)', file=sys.stderr)
            return []

        # Percentile ranks for confidence labels (across full common universe)
        pct_ranks = rv_now.rank(pct=True)

        signals: List[Signal] = []
        for ticker in candidates.index[: self.MAX_SIGNALS]:
            if ticker not in price_slice.columns:
                continue
            series = price_slice[ticker].dropna()
            if series.empty:
                continue
            current_price = float(series.iloc[-1])
            if current_price <= 0:
                continue

            raw_weight   = float(inv_vol[ticker] / inv_vol_sum)
            position_pct = float(min(raw_weight * scale, self.MAX_POS_PCT))

            pct = float(pct_ranks.get(ticker, 0.0))
            confidence = 'HIGH' if pct >= 0.95 else ('MED' if pct >= 0.85 else 'LOW')

            stops = self.compute_stops_and_targets(
                series, 'SHORT', current_price, regime_state=regime_state
            )

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = current_price,
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = position_pct,
                confidence        = confidence,
                signal_params     = {
                    'realized_vol_short': float(rv_now[ticker]),
                    'realized_vol_long':  float(rvl_now[ticker]),
                    'vol_excess':         float(vol_excess[ticker]),
                    'inv_vol_weight':     raw_weight,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
