"""
Market Rank Indicator Timing — SVD condition-number market-timing gate.

Computes the Market Rank Indicator (MRI) — a singular-value-decomposition
condition-number measure — over 11 Vanguard sector ETF daily return matrices.
When the monthly MRI percentile rank exceeds 75% (elevated systemic stress),
all exposure moves to cash (FLAT SPY). Otherwise stays LONG SPY (100%).
Rebalances on the first trading day of each calendar month.

Source: Roman, R. (2026). The Market Rank Indicator: Measuring Financial Risk,
Part 3. https://portfoliooptimizer.io/blog/the-market-rank-indicator-measuring-financial-risk-part-3/

Novelty: uses Figini et al. condition-number MRI (k=3 smallest singular values)
distinct from the Kritzman absorption ratio.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['MarketRankIndicatorTiming']

INSTRUMENT_CLASS = 'etp'

# 11 Vanguard sector ETFs used to build the return matrix
SECTOR_ETFS = ['VOX', 'VCR', 'VDC', 'VDE', 'VFH', 'VHT', 'VIS', 'VGT', 'VAW', 'VNQ', 'VPU']
MARKET_TICKER  = 'SPY'
MRI_K          = 3      # k smallest singular values for the geometric mean denominator
PCT_THRESHOLD  = 75.0  # percentile above which → FLAT (cash)
MIN_HIST_MONTHS = 12
MIN_MONTH_DAYS  = 10   # minimum trading days per calendar month for a valid MRI
MIN_ETF_COUNT   = 8    # graceful degradation: require at least this many sector ETFs


class MarketRankIndicatorTiming(BaseStrategy):
    """SVD condition-number MRI monthly market-timing gate on SPY.

    Monthly cycle:
      1. Collect daily arithmetic returns for available sector ETFs (T×n matrix).
      2. SVD: MRI_k = sigma_max / geomean(k smallest singular values), k=3.
      3. Rolling 12-month MRI history → percentile rank.
      4. rank > 75 → FLAT SPY (cash); else → LONG SPY.
    """

    id                = 'S_market_rank_indicator_timing'
    name              = 'Market Rank Indicator Timing'
    description       = (
        'SVD condition-number MRI over 11 sector ETFs: FLAT SPY when '
        'monthly MRI rank > 75th percentile, else LONG SPY.'
    )
    tier              = 2
    signal_frequency  = 'monthly'
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'mri_k':         MRI_K,
            'pct_threshold': PCT_THRESHOLD,
            'min_months':    MIN_HIST_MONTHS,
            'pos_size_frac': 0.95,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_mri(self, mat: np.ndarray) -> float:
        """MRI_k = sigma_max / geomean(sigma_1 … sigma_k) (ascending order)."""
        k = self.parameters['mri_k']
        if mat.shape[0] <= k or mat.shape[1] < k:
            return float('nan')
        try:
            _, svd_vals, _ = np.linalg.svd(mat, full_matrices=False)
        except np.linalg.LinAlgError:
            return float('nan')
        ascending = np.sort(svd_vals)
        k_smallest = ascending[:k]
        if np.any(k_smallest <= 0):
            return float('nan')
        geomean = float(np.exp(np.mean(np.log(k_smallest))))
        if geomean == 0.0:
            return float('nan')
        return float(ascending[-1] / geomean)

    def _monthly_mri_series(self, returns_df: pd.DataFrame) -> list:
        """One MRI value per complete calendar month in returns_df."""
        idx = pd.to_datetime(returns_df.index)
        periods = idx.to_period('M')
        mri_list: list[float] = []
        for period in sorted(set(periods)):
            mask = periods == period
            group = returns_df.values[mask]
            if group.shape[0] < MIN_MONTH_DAYS:
                continue
            mri = self._compute_mri(group)
            if not np.isnan(mri):
                mri_list.append(mri)
        return mri_list

    # ------------------------------------------------------------------
    # Core signal generation
    # ------------------------------------------------------------------

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

        # Monthly gate: fire only on the first trading day of each new month
        if len(prices) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        prices = prices.copy()
        prices.index = pd.to_datetime(prices.index)
        last_date = prices.index[-1]
        prev_date = prices.index[-2]
        if last_date.month == prev_date.month and last_date.year == prev_date.year:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Check sector ETF availability
        available_etfs = [t for t in SECTOR_ETFS if t in prices.columns]
        if len(available_etfs) < MIN_ETF_COUNT:
            print(
                f'[MRI] only {len(available_etfs)}/{len(SECTOR_ETFS)} ETFs in panel — need {MIN_ETF_COUNT}',
                file=sys.stderr,
            )
            print('[debug] signals=0', file=sys.stderr)
            return []

        if MARKET_TICKER not in prices.columns:
            print('[MRI] SPY not in price panel', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Restrict to data strictly before the current calendar month (no look-ahead)
        month_start = pd.Timestamp(last_date.year, last_date.month, 1)
        hist_prices = prices[prices.index < month_start]
        if len(hist_prices) < self.min_lookback:
            print(
                f'[MRI] {len(hist_prices)} bars before month-start, need {self.min_lookback}',
                file=sys.stderr,
            )
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Arithmetic daily returns for sector ETFs
        returns_df = hist_prices[available_etfs].pct_change().dropna()

        # Build monthly MRI series
        monthly_mri = self._monthly_mri_series(returns_df)
        min_months  = self.parameters.get('min_months', MIN_HIST_MONTHS)
        if len(monthly_mri) < min_months:
            print(
                f'[MRI] only {len(monthly_mri)} months of MRI history, need {min_months}',
                file=sys.stderr,
            )
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Percentile rank of current (most-recent) MRI vs last 12 months
        history     = monthly_mri[-min_months:]
        current_mri = history[-1]
        pct_rank    = float(np.sum(np.array(history) <= current_mri)) / len(history) * 100.0
        threshold   = self.parameters.get('pct_threshold', PCT_THRESHOLD)

        direction = 'FLAT' if pct_rank > threshold else 'LONG'

        # Build SPY bracket
        spy_series = prices[MARKET_TICKER].dropna()
        if len(spy_series) < 14:
            print('[debug] signals=0', file=sys.stderr)
            return []

        current_price = float(spy_series.iloc[-1])
        scale         = self.position_scale(regime_state)

        if direction == 'LONG':
            stops     = self.compute_stops_and_targets(
                spy_series, direction='LONG', current_price=current_price,
                regime_state=regime_state,
            )
            stop_loss = stops['stop']
            target_1  = stops['t1']
            target_2  = stops['t2']
            target_3  = stops['t3']
            pos_size  = round(self.parameters.get('pos_size_frac', 0.95) * scale, 4)
            confidence = 'MED'
        else:  # FLAT
            stop_loss = current_price
            target_1  = current_price
            target_2  = current_price
            target_3  = current_price
            pos_size  = 0.0
            confidence = 'HIGH'

        sig = Signal(
            ticker            = MARKET_TICKER,
            direction         = direction,
            entry_price       = current_price,
            stop_loss         = stop_loss,
            target_1          = target_1,
            target_2          = target_2,
            target_3          = target_3,
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'mri_current':  round(current_mri, 4),
                'pct_rank':     round(pct_rank, 2),
                'threshold':    threshold,
                'n_months':     len(monthly_mri),
                'n_etfs':       len(available_etfs),
                'regime':       regime_state,
                'mri_history':  [round(v, 4) for v in history],
            },
        )

        print(
            f'[MRI] direction={direction} mri={current_mri:.4f} pct_rank={pct_rank:.1f} '
            f'thr={threshold} spy={current_price:.2f} regime={regime_state} '
            f'etfs={len(available_etfs)} months={len(monthly_mri)}',
            file=sys.stderr,
        )
        print('[debug] signals=1', file=sys.stderr)
        return [sig]
