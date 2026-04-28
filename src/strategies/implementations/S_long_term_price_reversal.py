from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['LongTermPriceReversal']


class LongTermPriceReversal(BaseStrategy):
    """LONG 36-month prior losers, SHORT prior winners — De Bondt & Thaler (1985) overreaction reversal."""

    id               = 'S_long_term_price_reversal'
    name             = 'LongTermPriceReversal'
    description      = 'LONG 36-month prior losers, SHORT prior winners — behavioral overreaction reversal (De Bondt & Thaler 1985)'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 756  # ~36 months of trading days

    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    FORMATION_DAYS = 756   # ~36 calendar months of daily closes
    DECILE_FRAC    = 0.10  # top/bottom 10%
    BASE_SIZE      = 0.015 # 1.5% per position before regime scaling

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

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 20:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []
        if len(prices) < self.FORMATION_DAYS:
            print(f'[debug] signals=0 (insufficient history: {len(prices)} rows)', file=sys.stderr)
            return []

        window = prices[tickers].iloc[-self.FORMATION_DAYS:]
        start_px = window.iloc[0]
        end_px   = window.iloc[-1]
        cum_ret  = (end_px / start_px) - 1.0
        cum_ret  = cum_ret.replace([float('inf'), float('-inf')], float('nan')).dropna()

        if len(cum_ret) < 20:
            print(f'[debug] signals=0 (too few valid returns: {len(cum_ret)})', file=sys.stderr)
            return []

        n        = len(cum_ret)
        decile_n = max(1, int(n * self.DECILE_FRAC))
        ranked   = cum_ret.sort_values()
        rank_pct = cum_ret.rank(pct=True)

        losers  = ranked.iloc[:decile_n].index.tolist()   # bottom decile → LONG
        winners = ranked.iloc[-decile_n:].index.tolist()  # top decile → SHORT

        signals: List[Signal] = []
        half_cap = self.MAX_SIGNALS // 2

        for ticker in losers[:half_cap]:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            px = float(series.iloc[-1])
            if px <= 0:
                continue
            st = self.compute_stops_and_targets(series, 'LONG', px, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=px,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=float(self.BASE_SIZE * scale),
                confidence='MED',
                signal_params={
                    'cum_return_36m': float(cum_ret[ticker]),
                    'rank_pct': float(rank_pct[ticker]),
                    'formation_days': self.FORMATION_DAYS,
                },
            ))

        for ticker in winners[:half_cap]:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            px = float(series.iloc[-1])
            if px <= 0:
                continue
            st = self.compute_stops_and_targets(series, 'SHORT', px, regime_state=regime_state)
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=px,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=float(self.BASE_SIZE * scale),
                confidence='MED',
                signal_params={
                    'cum_return_36m': float(cum_ret[ticker]),
                    'rank_pct': float(rank_pct[ticker]),
                    'formation_days': self.FORMATION_DAYS,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
