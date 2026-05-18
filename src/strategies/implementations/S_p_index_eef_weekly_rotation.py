from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['PIndexEEFWeeklyRotation']


class PIndexEEFWeeklyRotation(BaseStrategy):
    """Long top-decile NYSE equities ranked by p-index (EEF efficiency score); weekly rebalance."""

    id               = 'S_p_index_eef_weekly_rotation'
    name             = 'PIndexEEFWeeklyRotation'
    description      = 'EEF p-index weekly rotation — LONG top-decile stocks by mean-variance efficiency score'
    tier             = 2
    signal_frequency = 'weekly'
    min_lookback     = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    LOOKBACK       = 504     # ~2 trading years
    DECILE_FRAC    = 0.10    # top 10% by p-index
    N_VOL_BINS     = 5       # quintile bins for EEF approximation
    BASE_SIZE      = 0.012   # fraction of NAV per position before regime scale
    SHARPE_WEIGHT  = 0.5     # weight on absolute Sharpe rank
    WITHIN_WEIGHT  = 0.5     # weight on within-vol-bin rank

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
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        tickers = [t for t in universe if t in prices.columns]

        if len(tickers) < self.parameters.get('min_universe', 50):
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[tickers].copy()
        if len(prices_sub) < self.LOOKBACK + 5:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        window = prices_sub.iloc[-self.LOOKBACK:]
        returns = window.pct_change().dropna()

        if len(returns) < 60:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # --- Compute p-index components ---
        mean_ret  = returns.mean()                         # daily mean
        real_vol  = returns.std(ddof=1).replace(0, np.nan)  # daily realized vol

        # Annualize
        ann_ret = mean_ret * 252
        ann_vol = real_vol * np.sqrt(252)

        valid = ann_vol.dropna()
        valid = valid[valid > 0]
        valid_tickers = list(valid.index)

        if len(valid_tickers) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        ann_ret_v = ann_ret[valid_tickers]
        ann_vol_v = ann_vol[valid_tickers]

        # Sharpe proxy (ranks stability over raw value)
        sharpe    = (ann_ret_v / ann_vol_v).rank(pct=True)

        # Within-vol-bin return rank (EEF residual approximation):
        # Bin stocks by realized vol quintile; rank by mean return within each bin.
        vol_series = ann_vol_v.copy()
        try:
            vol_bins = pd.qcut(vol_series, q=self.N_VOL_BINS, labels=False, duplicates='drop')
        except Exception:
            vol_bins = pd.Series(0, index=vol_series.index)

        within_rank = pd.Series(np.nan, index=valid_tickers)
        for b in vol_bins.dropna().unique():
            mask = vol_bins == b
            group = ann_ret_v[mask]
            if len(group) < 2:
                within_rank[group.index] = 0.5
            else:
                within_rank[group.index] = group.rank(pct=True)

        # Composite p-index score
        p_index = (self.SHARPE_WEIGHT * sharpe + self.WITHIN_WEIGHT * within_rank).dropna()

        if len(p_index) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Top decile by p-index → LONG
        cutoff    = p_index.quantile(1.0 - self.DECILE_FRAC)
        top_tickers = p_index[p_index >= cutoff].sort_values(ascending=False)

        current_prices = prices_sub.iloc[-1]
        size = round(self.BASE_SIZE * scale, 6)

        signals: List[Signal] = []
        for ticker, score in top_tickers.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw_price = current_prices.get(ticker)
            if raw_price is None or (raw_price != raw_price) or raw_price <= 0:
                continue
            price = float(raw_price)
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            if score >= 0.95:
                conf = 'HIGH'
            elif score >= 0.85:
                conf = 'MED'
            else:
                conf = 'LOW'
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size,
                confidence=conf,
                signal_params={
                    'p_index_score': round(float(score), 4),
                    'ann_ret': round(float(ann_ret_v.get(ticker, 0.0)), 4),
                    'ann_vol': round(float(ann_vol_v.get(ticker, 0.0)), 4),
                    'sharpe_rank': round(float(sharpe.get(ticker, 0.0)), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    def default_parameters(self) -> dict:
        return {'min_universe': 50}
