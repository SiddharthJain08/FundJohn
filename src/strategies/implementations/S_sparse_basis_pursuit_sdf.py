from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['SparseBasisPursuitSDF']


class SparseBasisPursuitSDF(BaseStrategy):
    """Sparse SDF via rank-based factor selection approximating L1-constrained basis pursuit."""

    id                = 'S_sparse_basis_pursuit_sdf'
    name              = 'SparseBasisPursuitSDF'
    description       = 'Sparse SDF estimation: rank-composite of price factors selects ~32 nonzero positions (approx L1 basis pursuit), monthly rebalance'
    tier              = 2
    min_lookback      = 63
    # Run in all regimes — spec has no regime_conditions
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    # Sparsity target: ~31-33 positions total (16 long + 16 short)
    N_POSITIONS = 16

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Filter to universe tickers present in prices
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        price_df = prices[tickers].ffill().dropna(how='all')
        if len(price_df) < 64:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        returns = price_df.pct_change()

        # --- Factor 1: 12-1 month momentum (Jegadeesh-Titman skip-1) ---
        factors = {}
        if len(price_df) >= 253:
            # skip last 21 days to avoid short-term reversal contamination
            p_now  = price_df.iloc[-22]
            p_then = price_df.iloc[-253]
            factors['mom_12_1'] = (p_now / p_then - 1).replace([np.inf, -np.inf], np.nan)

        # --- Factor 2: 3-month momentum ---
        if len(price_df) >= 63:
            p_now  = price_df.iloc[-1]
            p_then = price_df.iloc[-63]
            factors['mom_3m'] = (p_now / p_then - 1).replace([np.inf, -np.inf], np.nan)

        # --- Factor 3: 1-month reversal (negative — SDF loads negatively on recent losers) ---
        if len(price_df) >= 21:
            rev = returns.iloc[-21:].mean()
            factors['reversal'] = -rev  # invert so high score = strong reversal candidate

        # --- Factor 4: Low-volatility (inverted 21d std) ---
        vol = returns.iloc[-21:].std()
        vol_clean = vol.replace(0, np.nan)
        factors['low_vol'] = (-vol_clean).replace([np.inf, -np.inf], np.nan)

        # --- Factor 5: Short-term mean reversion (z-score distance from 20d mean) ---
        if len(price_df) >= 20:
            roll_mean = price_df.iloc[-20:].mean()
            last_px   = price_df.iloc[-1]
            zscore    = (last_px - roll_mean) / (vol_clean * np.sqrt(20) + 1e-8)
            factors['mean_rev'] = -zscore  # buy oversold, sell overbought

        if not factors:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        factor_df = pd.DataFrame(factors, index=price_df.columns)
        factor_df = factor_df.dropna(thresh=max(1, len(factors) - 1))  # allow 1 missing factor
        if len(factor_df) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Rank-normalize each factor (0-1, approximates L1 sparse weight selection)
        ranked    = factor_df.rank(pct=True)
        composite = ranked.mean(axis=1)  # equal-weight composite ~ SDF projection

        n_pos = min(self.N_POSITIONS, len(composite) // 4)
        if n_pos < 3:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Regime-adjusted position size: spread across sparse positions
        scale    = self.position_scale(regime_state)
        pos_size = round(scale * (1.0 / (n_pos * 2)), 4)  # equal-weight within sparsity budget
        pos_size = max(pos_size, 0.005)

        top_tickers = composite.nlargest(n_pos).index.tolist()
        bot_tickers = composite.nsmallest(n_pos).index.tolist()

        # Guard: ensure no overlap between long and short books
        bot_tickers = [t for t in bot_tickers if t not in top_tickers]

        last_prices = price_df.iloc[-1]
        signals: List[Signal] = []

        for ticker in top_tickers[: self.MAX_SIGNALS // 2]:
            series = price_df[ticker].dropna()
            if len(series) < 14:
                continue
            price  = float(last_prices[ticker])
            stops  = self.compute_stops_and_targets(series, 'LONG', price, regime_state=regime_state)
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = 'MED',
                signal_params     = {
                    'composite_score': round(float(composite[ticker]), 4),
                    'sdf_method':      'sparse_rank_l1_approx',
                    'n_factors':       len(factors),
                },
            ))

        for ticker in bot_tickers[: self.MAX_SIGNALS // 2]:
            series = price_df[ticker].dropna()
            if len(series) < 14:
                continue
            price  = float(last_prices[ticker])
            stops  = self.compute_stops_and_targets(series, 'SHORT', price, regime_state=regime_state)
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = 'MED',
                signal_params     = {
                    'composite_score': round(float(composite[ticker]), 4),
                    'sdf_method':      'sparse_rank_l1_approx',
                    'n_factors':       len(factors),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[: self.MAX_SIGNALS]
