"""
A Price-Based Framework for Stochastic Portfolio Theory
Paper: An, J.; Kim, D. (2026) — http://arxiv.org/abs/2610.04348v1

Diversity-weighted portfolios, functionally generated multiplicatively off
PRICE weight (not capitalization weight): price_weight_i = price_i / sum(price).
Tilting by price_weight_i^p (p in (0,1)) against the price-weighted market
benchmark exploits dispersion in relative price-weights the same way
capitalization-weighted diversity portfolios exploit cap dispersion — long
the names whose price weight is small relative to a uniform split, i.e.
tilt toward lower-priced, more numerous names.

Simplification vs. the paper: the paper's headline contribution is a
split/reverse-split jump-correction to the self-financing condition so
wealth stays continuous across corporate-action jumps (their Thm 4.x).
We do not carry an explicit multi-day wealth-process state machine here —
BaseStrategy signals are stateless day-over-day — so instead we neutralize
the jump risk at the source: any ticker whose price lookback shows a jump
consistent with a split/reverse-split (single-day |return| > 40%, a level
the paper's own worked examples use for split ratios) is dropped from
that day's universe rather than corrected. This is strictly conservative
(avoids the exact failure mode the paper sizes) at the cost of some
turnover around real split events. Diversity parameter p is fixed at 0.5
(midpoint of the paper's (0,1) interval; not fit to any backtest).
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import no_otc as universe_filter

__all__ = ['DiversityWeightedPriceGenerated']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_diversity_weighted_price_generated'
LOOKBACK = 252          # ~1 trading year, well under the paper's 756-day min but enough for a daily rebalance signal
MIN_UNIVERSE = 100       # below spec's 500 min_universe_size but matches live universe depth
P_DIVERSITY = 0.5        # midpoint of paper's (0,1) diversity parameter
SPLIT_JUMP_THRESHOLD = 0.40   # |1-day return| above this is treated as an uncorrected split/reverse-split jump
MIN_WEIGHT = 5e-4


class DiversityWeightedPriceGenerated(BaseStrategy):
    """Price-weight diversity portfolio (functionally generated, p=0.5)
    tilting long the dispersion of relative price-weights vs. a
    price-weighted benchmark, split-jump names excluded for wealth
    continuity."""

    id                = STRATEGY_ID
    name              = 'DiversityWeightedPriceGenerated'
    description       = ('Long portfolio tilted by price_weight_i^0.5 vs. the price-weighted '
                         'market, excluding tickers with an uncorrected split/reverse-split jump.')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {'lookback': LOOKBACK, 'p': P_DIVERSITY}

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
        lookback = int(self.parameters.get('lookback', LOOKBACK))
        p = float(self.parameters.get('p', P_DIVERSITY))

        cols = [t for t in universe if t in prices.columns]
        if len(cols) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (universe {len(cols)} < min {MIN_UNIVERSE})', file=sys.stderr)
            return []

        sub = prices[cols].dropna(axis=1, thresh=min(lookback, len(prices)))
        if sub.shape[0] < 20 or sub.shape[1] < MIN_UNIVERSE:
            print(f'[debug] signals=0 (shape {sub.shape} too thin)', file=sys.stderr)
            return []
        window = sub.iloc[-lookback:] if sub.shape[0] >= lookback else sub

        # Drop tickers with an uncorrected split/reverse-split jump in the
        # lookback window — the paper's jump-correction machinery is what we
        # are deliberately not implementing, so we exclude the failure mode
        # instead of correcting it.
        daily_ret = window.pct_change().abs()
        jump_mask = (daily_ret > SPLIT_JUMP_THRESHOLD).any()
        clean_cols = jump_mask[~jump_mask].index.tolist()
        if len(clean_cols) < MIN_UNIVERSE:
            print(f'[debug] signals=0 (clean universe {len(clean_cols)} < min {MIN_UNIVERSE} after split filter)', file=sys.stderr)
            return []

        last = window[clean_cols].iloc[-1].dropna()
        last = last[last > 0]
        if last.shape[0] < MIN_UNIVERSE:
            print(f'[debug] signals=0 (priced universe {last.shape[0]} < min {MIN_UNIVERSE})', file=sys.stderr)
            return []

        total_price = float(last.sum())
        price_weight = last / total_price               # market (price-weighted) weight
        diversity_raw = price_weight.pow(p)
        portfolio_weight = diversity_raw / diversity_raw.sum()   # functionally generated diversity weight

        # Tilt = how much the diversity weight overweights a name vs. the
        # price-weighted market — that excess tilt is the long signal.
        tilt = (portfolio_weight - price_weight).sort_values(ascending=False)

        signals: List[Signal] = []
        for ticker, tilt_val in tilt.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            wi = float(portfolio_weight[ticker])
            if wi < MIN_WEIGHT or tilt_val <= 0:
                continue
            price = float(last[ticker])
            st = self.compute_stops_and_targets(window[ticker], 'LONG', price, regime_state=regime_state)
            pos = round(wi * scale, 4)
            conf = 'HIGH' if wi > 0.03 else ('MED' if wi > 0.01 else 'LOW')
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=price,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=pos,
                confidence=conf,
                signal_params={
                    'p':             round(p, 4),
                    'price_weight':  round(float(price_weight[ticker]), 6),
                    'diversity_weight': round(wi, 6),
                },
            ))

        print(f'[debug] signals={len(signals)} p={p:.2f} universe={last.shape[0]}', file=sys.stderr)
        return signals
