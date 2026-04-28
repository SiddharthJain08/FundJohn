from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['CrossSectionalPriceMomentum']


class CrossSectionalPriceMomentum(BaseStrategy):
    """Cross-sectional momentum: LONG top-decile, SHORT bottom-decile (Jegadeesh & Titman 1993)."""

    id               = 'S_cross_sectional_price_momentum'
    name             = 'CrossSectionalPriceMomentum'
    description      = 'J&T cross-sectional momentum — LONG 12-1mo winners, SHORT losers (Jegadeesh & Titman 1993)'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    # 12-month formation, skip most recent month
    FORMATION_DAYS = 252
    SKIP_DAYS      = 21
    DECILE_FRAC    = 0.10
    BASE_SIZE_LONG  = 0.015
    BASE_SIZE_SHORT = 0.012

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

        # Filter to available universe tickers
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[tickers]
        min_rows = self.FORMATION_DAYS + self.SKIP_DAYS + 5
        if len(prices_sub) < min_rows:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # J&T momentum: cumulative return from (t - J - skip) to (t - skip)
        end_idx   = len(prices_sub) - 1 - self.SKIP_DAYS
        start_idx = end_idx - self.FORMATION_DAYS
        if start_idx < 0:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        p_end   = prices_sub.iloc[end_idx]
        p_start = prices_sub.iloc[start_idx].replace(0, float('nan'))
        momentum = ((p_end - p_start) / p_start).dropna()

        if len(momentum) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Percentile rank: 0 = lowest momentum, 1 = highest
        rank_pct = momentum.rank(pct=True)

        winners = rank_pct[rank_pct >= (1.0 - self.DECILE_FRAC)].sort_values(ascending=False)
        losers  = rank_pct[rank_pct <= self.DECILE_FRAC].sort_values(ascending=True)

        current_prices = prices_sub.iloc[-1]
        size_long  = round(self.BASE_SIZE_LONG  * scale, 6)
        size_short = round(self.BASE_SIZE_SHORT * scale, 6)

        def conf(pct: float, long: bool) -> str:
            extreme = pct if long else (1.0 - pct)
            if extreme >= 0.95:
                return 'HIGH'
            elif extreme >= 0.90:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []

        for ticker, rp in winners.items():
            if len(signals) >= self.MAX_SIGNALS // 2:
                break
            raw_price = current_prices.get(ticker)
            if raw_price is None or raw_price != raw_price or raw_price <= 0:
                continue
            price = float(raw_price)
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size_long,
                confidence=conf(float(rp), long=True),
                signal_params={
                    'momentum_12m_1m': round(float(momentum[ticker]), 4),
                    'rank_pct': round(float(rp), 4),
                },
            ))

        for ticker, rp in losers.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw_price = current_prices.get(ticker)
            if raw_price is None or raw_price != raw_price or raw_price <= 0:
                continue
            price = float(raw_price)
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'SHORT', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size_short,
                confidence=conf(float(rp), long=False),
                signal_params={
                    'momentum_12m_1m': round(float(momentum[ticker]), 4),
                    'rank_pct': round(float(rp), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
