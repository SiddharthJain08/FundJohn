from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

# https://quantpedia.com/strategies/momentum-factor-and-style-rotation-effect/
INSTRUMENT_CLASS = 'etp'

__all__ = ['MomentumFactorStyleRotation']

ETF_UNIVERSE = ['IWS', 'IWP', 'IWN', 'IWO', 'IVE', 'IVW']
MOM_PERIOD   = 252   # 12-month momentum (~252 trading days)


class MomentumFactorStyleRotation(BaseStrategy):
    """Long top 12-month momentum equity-style ETF, short the bottom; monthly rebalance.

    Source: https://quantpedia.com/strategies/momentum-factor-and-style-rotation-effect/
    Universe: IWS, IWP, IWN, IWO, IVE, IVW (Russell/S&P style ETFs).
    Each month rank by 252-day price return. Long winner, short loser, flat middle 4.
    """

    id               = 'S_ast_momentum_factor_and_style_rotation_effect'
    name             = 'MomentumFactorStyleRotation'
    description      = ('The top 12-month momentum equity-style ETF outperforms the bottom; '
                        'go long the winner and short the loser, rebalancing monthly.')
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = MOM_PERIOD + 5
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

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
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Monthly rebalance: only fire on the first bar of a new calendar month
        if len(prices) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        last_date = pd.Timestamp(prices.index[-1])
        prev_date = pd.Timestamp(prices.index[-2])
        if last_date.month == prev_date.month:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Filter to ETFs present in prices
        available = [t for t in ETF_UNIVERSE if t in prices.columns]
        if len(available) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if len(prices) < MOM_PERIOD + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # 12-month momentum: (price_now / price_252_bars_ago) - 1
        current_row = prices[available].iloc[-1]
        past_row    = prices[available].iloc[-(MOM_PERIOD + 1)]

        mom: dict[str, float] = {}
        for t in available:
            cp = current_row.get(t)
            pp = past_row.get(t)
            if cp is None or pp is None or pd.isna(cp) or pd.isna(pp) or float(pp) == 0.0:
                continue
            mom[t] = float(cp) / float(pp) - 1.0

        if len(mom) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ranked = sorted(mom, key=lambda x: mom[x], reverse=True)
        winner = ranked[0]
        loser  = ranked[-1]
        middle = ranked[1:-1]

        scale   = self.position_scale(regime_state)
        signals: List[Signal] = []

        # Long winner (100% gross, scaled by regime)
        w_price = float(current_row[winner])
        if w_price > 0:
            stops = self.compute_stops_and_targets(
                prices[winner].dropna(), 'LONG', w_price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=winner,
                direction='LONG',
                entry_price=w_price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(1.0 * scale, 4),
                confidence='HIGH',
                signal_params={
                    'momentum_12m': round(mom[winner], 4),
                    'rank': 1,
                    'n_ranked': len(ranked),
                },
            ))

        # Short loser (100% gross, scaled by regime)
        l_price = float(current_row[loser])
        if l_price > 0:
            stops = self.compute_stops_and_targets(
                prices[loser].dropna(), 'SHORT', l_price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=loser,
                direction='SHORT',
                entry_price=l_price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(1.0 * scale, 4),
                confidence='HIGH',
                signal_params={
                    'momentum_12m': round(mom[loser], 4),
                    'rank': len(ranked),
                    'n_ranked': len(ranked),
                },
            ))

        # Flat middle tickers (liquidate existing positions)
        for t in middle:
            m_price = float(current_row[t])
            if m_price > 0:
                signals.append(Signal(
                    ticker=t,
                    direction='FLAT',
                    entry_price=m_price,
                    stop_loss=0.0,
                    target_1=0.0,
                    target_2=0.0,
                    target_3=0.0,
                    position_size_pct=0.0,
                    confidence='MED',
                    signal_params={
                        'momentum_12m': round(mom.get(t, 0.0), 4),
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
