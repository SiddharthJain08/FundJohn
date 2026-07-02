# Source: https://quantpedia.com/strategies/short-term-reversal-in-stocks/
# Clean-room re-implementation for the FundJohn BaseStrategy contract.
# Original reference: QuantConnect LEAN / paperswithbacktest/awesome-systematic-trading
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import sp500 as universe_filter

INSTRUMENT_CLASS = 'equity'

__all__ = ['AstShortTermReversalInStocks']


class AstShortTermReversalInStocks(BaseStrategy):
    """Weekly cross-sectional reversal: long 10 worst 1-week losers, short 10 best 1-month winners.

    Source: https://quantpedia.com/strategies/short-term-reversal-in-stocks/
    Universe: top 100 US large-cap stocks (sp500 filter proxy), price > $1.
    Rebalance: every 5 trading days.
    Long: 10 stocks with lowest 1-week return (close[0]/close[5]-1).
    Short: 10 stocks with highest 1-month return (close[0]/close[20]-1), excluding long leg.
    Sizing: equal-weight, 10% per position (5x leveraged; net market-neutral).
    """

    id               = 'S_ast_short_term_reversal_in_stocks'
    name             = 'AstShortTermReversalInStocks'
    description      = ('Large-cap stocks that underperformed last week mean-revert upward, '
                        'while last month\'s winners revert downward — exploiting short-term '
                        'price reversal in the top 100 US stocks by market cap.')
    tier             = 2
    signal_frequency = 'weekly'
    min_lookback     = 21
    # Mean-reversion works across regimes; widen to all but CRISIS where spreads blow out
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    REBAL_PERIOD   = 5     # trading days between rebalances
    LOOKBACK_WEEK  = 5     # bars for 1-week return
    LOOKBACK_MONTH = 20    # bars for 1-month return
    TOP_N_UNIVERSE = 100   # top-N by rank (proxy for market cap via recency of universe)
    STOCK_SELECT   = 10    # positions per leg
    MIN_PRICE      = 1.0   # penny-stock filter

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

        # Filter to universe tickers present in prices
        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Need at least LOOKBACK_MONTH + 1 bars
        min_bars = self.LOOKBACK_MONTH + 1
        if len(prices) < min_bars:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Weekly rebalance gate: only fire every REBAL_PERIOD bars
        # Use row count mod REBAL_PERIOD as a proxy for "is it rebalance day"
        if len(prices) % self.REBAL_PERIOD != 0:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Build return table for eligible tickers
        eligible = []
        for ticker in tickers:
            series = prices[ticker].dropna()
            if len(series) < min_bars:
                continue
            current_price = float(series.iloc[-1])
            if current_price < self.MIN_PRICE:
                continue

            price_now   = float(series.iloc[-1])
            price_1w    = float(series.iloc[-self.LOOKBACK_WEEK - 1]) if len(series) > self.LOOKBACK_WEEK else None
            price_1m    = float(series.iloc[-self.LOOKBACK_MONTH - 1]) if len(series) > self.LOOKBACK_MONTH else None

            if price_1w is None or price_1m is None or price_1w == 0 or price_1m == 0:
                continue

            ret_1w = price_now / price_1w - 1.0
            ret_1m = price_now / price_1m - 1.0
            eligible.append((ticker, current_price, ret_1w, ret_1m))

        if len(eligible) < self.STOCK_SELECT * 2:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Narrow to top-N by proxy for market cap:
        # We rank by the absolute price level as a crude size proxy (larger price ≈ larger cap
        # in a filtered large-cap universe). This mirrors the QC implementation's coarse→fine
        # two-pass without access to real market-cap data.
        eligible.sort(key=lambda x: x[1], reverse=True)
        top_n = eligible[:self.TOP_N_UNIVERSE]

        # Sort by 1-week return ascending → long the worst performers (reversal candidates)
        sorted_by_week = sorted(top_n, key=lambda x: x[2])
        long_set = sorted_by_week[:self.STOCK_SELECT]
        long_tickers = {item[0] for item in long_set}

        # Sort by 1-month return descending → short the best performers (mean-revert down)
        sorted_by_month = sorted(top_n, key=lambda x: x[3], reverse=True)
        short_items = []
        for item in sorted_by_month:
            if item[0] not in long_tickers:
                short_items.append(item)
            if len(short_items) == self.STOCK_SELECT:
                break

        if not long_set or not short_items:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Equal-weight per leg: 1/10 = 10% per position, scaled by regime
        per_position = round(0.10 * scale, 6)

        signals: List[Signal] = []

        for ticker, price, ret_1w, ret_1m in long_set:
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=float(price),
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']),
                target_2=float(stops['t2']),
                target_3=float(stops['t3']),
                position_size_pct=per_position,
                confidence='MED',
                signal_params={
                    'ret_1w': round(ret_1w, 6),
                    'ret_1m': round(ret_1m, 6),
                    'leg': 'long_reversal',
                },
            ))

        for ticker, price, ret_1w, ret_1m in short_items:
            stops = self.compute_stops_and_targets(
                prices[ticker].dropna(), 'SHORT', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=float(price),
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']),
                target_2=float(stops['t2']),
                target_3=float(stops['t3']),
                position_size_pct=per_position,
                confidence='MED',
                signal_params={
                    'ret_1w': round(ret_1w, 6),
                    'ret_1m': round(ret_1m, 6),
                    'leg': 'short_reversal',
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
