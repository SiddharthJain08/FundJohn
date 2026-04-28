"""
Momentum Delayed Overreaction — Jegadeesh & Titman (2001)
"Profitability of Momentum Strategies: An Evaluation of Alternative Explanations"
Journal of Finance, 56(2), 699-720. https://doi.org/10.1111/0022-1082.00342

Signal: 12-1 cross-sectional momentum (cumulative return t-12m to t-1m, skip most
recent month). LONG top decile, SHORT bottom decile. Thesis: investor delayed
overreaction creates persistent 6-month price drift before eventual long-horizon reversal.
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['MomentumDelayedOverreaction']


class MomentumDelayedOverreaction(BaseStrategy):
    """12-1 cross-sectional momentum: LONG top decile, SHORT bottom decile (JT 2001)."""

    id                = 'S_momentum_delayed_overreaction'
    name              = 'MomentumDelayedOverreaction'
    description       = ('Past 12-month winners (skip 1m) outperform losers over next 6m '
                         'due to investor delayed overreaction (Jegadeesh & Titman 2001)')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 504          # ~2 trading years for stability
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    FORMATION_DAYS    = 252          # 12-month lookback
    SKIP_DAYS         = 21           # skip most recent month (reversal avoidance)
    DECILE_FRAC       = 0.10         # top/bottom 10%
    MIN_UNIVERSE      = 20           # minimum tickers with valid momentum
    BASE_SIZE_PER_LEG = 0.015        # 1.5% per position per leg before regime scale

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
            print(f'[debug] signals=0 (regime={regime_state} not active)', file=sys.stderr)
            return []

        if len(prices) < self.FORMATION_DAYS + self.SKIP_DAYS:
            print(f'[debug] signals=0 (insufficient history: {len(prices)} rows)', file=sys.stderr)
            return []

        # Filter to universe tickers in the wide-format prices DataFrame
        available = [
            t for t in universe
            if t in prices.columns
            and not t.startswith('^')
            and not t.endswith('=F')
            and '-USD' not in t
        ]
        if len(available) < self.MIN_UNIVERSE:
            print(f'[debug] signals=0 (available={len(available)} < {self.MIN_UNIVERSE})', file=sys.stderr)
            return []

        # Compute 12-1 momentum per ticker on its own non-NaN trading days
        momentum: dict[str, float] = {}
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < self.FORMATION_DAYS + self.SKIP_DAYS:
                continue
            p_start = float(series.iloc[-(self.FORMATION_DAYS + self.SKIP_DAYS)])
            p_end   = float(series.iloc[-self.SKIP_DAYS])
            if p_start <= 0:
                continue
            momentum[ticker] = (p_end - p_start) / p_start

        if len(momentum) < self.MIN_UNIVERSE:
            print(f'[debug] signals=0 (valid momentum={len(momentum)} < {self.MIN_UNIVERSE})', file=sys.stderr)
            return []

        ranked    = sorted(momentum.items(), key=lambda x: x[1])
        n         = len(ranked)
        decile_n  = max(1, int(n * self.DECILE_FRAC))

        losers  = [t for t, _ in ranked[:decile_n]]
        winners = [t for t, _ in ranked[-decile_n:]]

        scale    = self.position_scale(regime_state)
        pos_size = round(self.BASE_SIZE_PER_LEG * scale, 4)

        signals: List[Signal] = []

        for ticker, direction in [(t, 'LONG') for t in winners] + [(t, 'SHORT') for t in losers]:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            current_price = float(series.iloc[-1])
            stops = self.compute_stops_and_targets(series, direction, current_price, regime_state=regime_state)

            mom_val  = momentum[ticker]
            mom_rank = sum(1 for v in momentum.values() if v <= mom_val) / n

            if direction == 'LONG':
                confidence = 'HIGH' if mom_rank >= 0.95 else ('MED' if mom_rank >= 0.90 else 'LOW')
            else:
                confidence = 'HIGH' if mom_rank <= 0.05 else ('MED' if mom_rank <= 0.10 else 'LOW')

            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = current_price,
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'momentum_12_1':  round(mom_val, 4),
                    'momentum_rank':  round(mom_rank, 4),
                    'formation_days': self.FORMATION_DAYS,
                    'skip_days':      self.SKIP_DAYS,
                    'regime':         regime_state,
                    'scale':          scale,
                },
            ))

            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
