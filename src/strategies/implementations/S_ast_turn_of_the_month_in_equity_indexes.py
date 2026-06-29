"""
Turn of the Month in Equity Indexes — QuantPedia strategy port.

Source: https://quantpedia.com/strategies/turn-of-the-month-in-equity-indexes/

Go 100% long SPY on the last trading day of each month.
Exit at the close of the 3rd trading day of the following month.
All other days are flat.

Reference impl (QuantConnect / LEAN) translated clean-room to BaseStrategy contract.
"""
from __future__ import annotations

import os
import sys
import pandas as pd
from pandas.tseries.offsets import BMonthEnd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstTurnOfTheMonthInEquityIndexes']

INSTRUMENT_CLASS     = 'etp'
STRATEGY_ID          = 'S_ast_turn_of_the_month_in_equity_indexes'
TICKER               = 'SPY'
HOLD_DAYS_NEW_MONTH  = 3   # exit on close of 3rd trading day of new month


class AstTurnOfTheMonthInEquityIndexes(BaseStrategy):
    """Long SPY on the last trading day of each month; exit on the 3rd trading day of the next month.

    Exploits the well-documented turn-of-month effect: institutional cash flows and
    portfolio rebalancing concentrate buying pressure around month-end / month-start,
    generating a persistent positive return anomaly over ~4 trading days.
    """

    id                = STRATEGY_ID
    name              = 'Turn of the Month in Equity Indexes'
    description       = (
        'Long SPY on last trading day of each month; '
        'exit on 3rd trading day of next month.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 10
    # Calendar effect documented across all market regimes.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'hold_days_new_month': HOLD_DAYS_NEW_MONTH,
            'pos_size_frac':       0.95,   # near-full allocation to SPY
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
        if TICKER not in prices.columns:
            print(f'[TurnOfMonth] SPY not in prices — returning []', file=sys.stderr)
            return []

        spy = prices[TICKER].dropna()
        if len(spy) < 10:
            return []

        today_ts      = pd.Timestamp(spy.index[-1])
        current_price = float(spy.iloc[-1])

        # Trading day of the current month (1-indexed)
        this_month = spy.index[
            (spy.index.month == today_ts.month) & (spy.index.year == today_ts.year)
        ]
        try:
            today_pos_in_month = list(this_month).index(spy.index[-1]) + 1
        except ValueError:
            today_pos_in_month = len(this_month)

        # Last business day of the month (BMonthEnd approx; misses non-standard holidays
        # but is accurate for SPY on >99% of trading days).
        is_last_trading_day = ((today_ts + BMonthEnd(0)).date() == today_ts.date())

        hold_days = self.parameters.get('hold_days_new_month', HOLD_DAYS_NEW_MONTH)
        scale     = self.position_scale(regime_state)
        pos_frac  = self.parameters.get('pos_size_frac', 0.95)

        stops = self.compute_stops_and_targets(
            spy,
            direction     = 'LONG',
            current_price = current_price,
            regime_state  = regime_state,
        )

        signals: List[Signal] = []

        if is_last_trading_day:
            signals.append(Signal(
                ticker            = TICKER,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(pos_frac * scale, 4),
                confidence        = 'HIGH',
                signal_params     = {
                    'calendar_event':       'month_end_entry',
                    'trading_day_of_month': today_pos_in_month,
                    'regime':               regime_state,
                },
            ))
        elif today_pos_in_month == hold_days:
            signals.append(Signal(
                ticker            = TICKER,
                direction         = 'FLAT',
                entry_price       = current_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = 0.0,
                confidence        = 'HIGH',
                signal_params     = {
                    'calendar_event':       'month_start_exit',
                    'trading_day_of_month': today_pos_in_month,
                    'regime':               regime_state,
                },
            ))

        print(
            f'[TurnOfMonth] date={today_ts.date()} day_in_month={today_pos_in_month} '
            f'is_last={is_last_trading_day} signals={len(signals)}',
            file=sys.stderr,
        )
        return signals


def _run_backtest() -> pd.DataFrame:
    """Simulate turn-of-month trades over full SPY history; return trades DataFrame."""
    root = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master')
    spy = pd.read_parquet(os.path.join(root, 'prices.parquet'), columns=['SPY'])['SPY'].dropna().sort_index()
    regimes = pd.read_parquet(os.path.join(root, 'historical_regimes.parquet'))
    regime_col = 'state' if 'state' in regimes.columns else regimes.columns[0]
    regimes = regimes[[regime_col]].rename(columns={regime_col: 'regime_state'})

    rows = []
    dates = list(spy.index)
    for i, date in enumerate(dates):
        ts = pd.Timestamp(date)
        if (ts + BMonthEnd(0)).date() != ts.date():
            continue   # not the last business day of the month

        entry_price = float(spy.iloc[i])

        # Find 3rd trading day of the following month
        next_month = None
        count = 0
        exit_idx = None
        for j in range(i + 1, min(i + 30, len(dates))):
            d = pd.Timestamp(dates[j])
            if next_month is None and (d.month != ts.month or d.year != ts.year):
                next_month = (d.year, d.month)
            if next_month and (d.year, d.month) == next_month:
                count += 1
                if count == HOLD_DAYS_NEW_MONTH:
                    exit_idx = j
                    break

        if exit_idx is None:
            continue

        exit_price = float(spy.iloc[exit_idx])
        pnl = (exit_price - entry_price) / entry_price

        # Regime at signal date
        regime_state = 'LOW_VOL'
        pos = regimes.index.searchsorted(date)
        if pos > 0:
            regime_state = str(regimes.iloc[pos - 1]['regime_state'])

        rows.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  str(date.date()) if hasattr(date, 'date') else str(date),
            'regime_state': regime_state,
            'pnl':          float(pnl),
            'r_multiple':   float(pnl / 0.02),
        })

    return pd.DataFrame(rows)


if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition

    trades = _run_backtest()
    print(f'[TurnOfMonth] {len(trades)} trades generated', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades,
        strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(result)
