"""
Paired Switching — ported from QuantConnect / Quantpedia implementation.

Source: https://quantpedia.com/strategies/paired-switching/

Hold 100% in whichever of SPY or AGG had higher trailing-quarter return;
switch quarterly — exploiting the negative equity/bond correlation to rotate
into the recent winner.

Basket: SPY (equity proxy), AGG (bond proxy).
Rebalance: first trading bar of months 3, 6, 9, 12 (quarterly).
Ranking period: trailing 90 calendar days.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstPairedSwitching']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_ast_paired_switching'

# Fixed 2-ETF basket per Quantpedia spec
BASKET           = ('SPY', 'AGG')
LOOKBACK_DAYS    = 90   # calendar days for trailing return
QUARTERLY_MONTHS = {3, 6, 9, 12}


class AstPairedSwitching(BaseStrategy):
    """Quarterly rotation: 100 % to SPY or AGG, whichever had higher 90-day return.

    On the first bar of each quarterly month (Mar/Jun/Sep/Dec) the strategy
    ranks SPY vs AGG by trailing 90-calendar-day total return and allocates
    the full portfolio to the winner; emits FLAT for the loser to trigger an
    exit on the next execution cycle.  Non-rebalance days return an empty list.
    """

    id                = STRATEGY_ID
    name              = 'Paired Switching'
    description       = (
        'Hold 100% in whichever of SPY or AGG had higher trailing-quarter return; '
        'switch quarterly — exploiting the negative equity/bond correlation.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 70          # ~65 trading days ≈ 90 calendar days + buffer
    # All-weather rotation between equities and bonds — effective in any regime.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {
            'lookback_days': LOOKBACK_DAYS,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        # Only rebalance on the first trading bar of quarterly months.
        if len(prices.index) < 2:
            return []
        current_month = prices.index[-1].month
        prev_month    = prices.index[-2].month
        if current_month not in QUARTERLY_MONTHS or current_month == prev_month:
            return []

        lookback_days = int(self.parameters.get('lookback_days', LOOKBACK_DAYS))

        # Verify both tickers are present.
        spy_in  = 'SPY' in prices.columns
        agg_in  = 'AGG' in prices.columns
        if not spy_in or not agg_in:
            missing = [t for t in BASKET if t not in prices.columns]
            print(f'[AstPairedSwitching] missing tickers {missing} — returning []', file=sys.stderr)
            return []

        current_date = prices.index[-1]
        start_date   = current_date - pd.Timedelta(days=lookback_days)

        # Slice to the 90-calendar-day window.
        window = prices[prices.index >= start_date]
        if len(window) < 2:
            print(f'[AstPairedSwitching] window too short ({len(window)} bars) — returning []', file=sys.stderr)
            return []

        scale   = self.position_scale(regime_state)
        signals: List[Signal] = []
        returns: dict[str, float] = {}

        for ticker in BASKET:
            series = window[ticker].dropna()
            if len(series) < 2:
                print(f'[AstPairedSwitching] {ticker} insufficient window data — returning []', file=sys.stderr)
                return []
            price_start   = float(series.iloc[0])
            price_current = float(series.iloc[-1])
            if price_current == 0:
                return []
            # Quantpedia return formula: (current - start) / current
            returns[ticker] = (price_current - price_start) / price_current

        # Winner = ETF with higher trailing return.
        winner = max(returns, key=lambda t: returns[t])
        loser  = [t for t in BASKET if t != winner][0]

        for ticker in BASKET:
            series        = prices[ticker].dropna()
            current_price = float(series.iloc[-1])

            if ticker == winner:
                stops = self.compute_stops_and_targets(
                    series,
                    direction     = 'LONG',
                    current_price = current_price,
                    regime_state  = regime_state,
                )
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = 'LONG',
                    entry_price       = current_price,
                    stop_loss         = stops['stop'],
                    target_1          = stops['t1'],
                    target_2          = stops['t2'],
                    target_3          = stops['t3'],
                    position_size_pct = round(1.0 * scale, 4),
                    confidence        = 'HIGH',
                    signal_params     = {
                        'trailing_return': round(returns[ticker], 6),
                        'loser_return':    round(returns[loser], 6),
                        'winner':          winner,
                        'lookback_days':   lookback_days,
                        'regime':          regime_state,
                        'scale':           scale,
                    },
                ))
            else:
                # Emit FLAT for the loser so the execution engine closes any open position.
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = 'FLAT',
                    entry_price       = current_price,
                    stop_loss         = current_price * 0.95,
                    target_1          = current_price,
                    target_2          = current_price,
                    target_3          = current_price,
                    position_size_pct = 0.0,
                    confidence        = 'LOW',
                    signal_params     = {
                        'trailing_return': round(returns[ticker], 6),
                        'winner':          winner,
                        'reason':          'quarterly_switch_to_winner',
                        'regime':          regime_state,
                    },
                ))

        print(
            f'[AstPairedSwitching] rebalance winner={winner} '
            f'SPY={returns["SPY"]:.4f} AGG={returns["AGG"]:.4f} '
            f'signals={len(signals)} regime={regime_state}',
            file=sys.stderr,
        )
        return signals
