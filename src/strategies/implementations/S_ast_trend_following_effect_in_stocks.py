# Source: https://quantpedia.com/strategies/trend-following-effect-in-stocks/
# Clean-room re-implementation for the FundJohn BaseStrategy contract.
# Original reference: QuantConnect LEAN / paperswithbacktest/awesome-systematic-trading
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import no_otc as universe_filter

INSTRUMENT_CLASS = 'equity'

__all__ = ['AstTrendFollowingEffectInStocks']


class AstTrendFollowingEffectInStocks(BaseStrategy):
    """ATH-breakout trend following: enter when close >= all-time high, trail ATR(10) stop.

    Source: https://quantpedia.com/strategies/trend-following-effect-in-stocks/
    Universe: top liquid US equities, price > $5, no-OTC filter.
    Entry: today's close >= max close over entire available history.
    Exit: trailing ATR(10) stop (close - ATR10); no fixed profit target.
    Sizing: equal-weight across all signals, capped at MAX_BASKET positions.
    """

    id               = 'S_ast_trend_following_effect_in_stocks'
    name             = 'AstTrendFollowingEffectInStocks'
    description      = ('Stocks hitting all-time-high closes exhibit momentum continuation; '
                        'enter on ATH breakout and trail a 10-period ATR stop to ride the trend.')
    tier             = 2
    signal_frequency = 'daily'
    min_lookback     = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    MIN_PRICE  = 5.0    # penny-stock filter
    ATR_PERIOD = 10
    MAX_BASKET = 40     # cap equal-weight basket size
    BASE_SIZE  = 0.025  # max per-position at full regime scale

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
        if not tickers:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Identify ATH breakout candidates
        candidates = []
        for ticker in tickers:
            series = prices[ticker].dropna()
            if len(series) < self.ATR_PERIOD + 5:
                continue

            current_close = float(series.iloc[-1])
            if current_close < self.MIN_PRICE:
                continue

            # ATH signal: today's close must equal the historical maximum
            hist_max = float(series.max())
            if current_close < hist_max:
                continue

            # ATR(10) proxy using close-to-close abs diff
            atr_raw = series.diff().abs().rolling(self.ATR_PERIOD).mean().iloc[-1]
            atr_val = float(atr_raw) if (pd.notna(atr_raw) and float(atr_raw) > 0) else current_close * 0.02

            candidates.append((ticker, current_close, atr_val))

        if not candidates:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Equal-weight across all candidates, capped at MAX_BASKET
        basket = candidates[:self.MAX_BASKET]
        n = len(basket)
        per_position = round(min(1.0 / n, self.BASE_SIZE) * scale, 6)

        signals: List[Signal] = []
        for ticker, price, atr_val in basket:
            trailing_stop = price - atr_val
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=per_position,
                confidence='HIGH',
                signal_params={
                    'atr10': round(atr_val, 4),
                    'trailing_stop': round(trailing_stop, 4),
                    'hist_max': round(float(prices[ticker].dropna().max()), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
