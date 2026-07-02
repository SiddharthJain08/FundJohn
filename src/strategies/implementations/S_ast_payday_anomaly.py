"""
Payday Anomaly — QuantPedia strategy port.

Source: https://quantpedia.com/strategies/payday-anomaly/

SPY earns outsized returns on the monthly payday (15th calendar day, or the
prior Friday if the 15th falls on a weekend). Enter LONG at close of the
payday; exit at close of the very next trading day. All other days are flat.

Reference impl (QuantConnect / LEAN) translated clean-room to BaseStrategy
contract.
"""
from __future__ import annotations

import os
import sys
import pandas as pd
from datetime import date, timedelta
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstPaydayAnomaly']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_ast_payday_anomaly'
TICKER           = 'SPY'


def _payday_date(year: int, month: int) -> date:
    """Return the payday date for the given month: 15th, shifted to Friday if weekend."""
    d = date(year, month, 15)
    wd = d.weekday()
    if wd == 5:   # Saturday → Friday
        d -= timedelta(days=1)
    elif wd == 6: # Sunday → Friday
        d -= timedelta(days=2)
    return d


class AstPaydayAnomaly(BaseStrategy):
    """Long SPY on the monthly payday (15th or prior Friday); exit the next trading day.

    Hypothesis: systematic payroll-driven inflows on or around the 15th of each
    month create temporary upward price pressure on broad equity ETFs.
    """

    id                = STRATEGY_ID
    name              = 'AST Payday Anomaly'
    description       = (
        'SPY earns outsized returns on the monthly payday (15th or prior business day), '
        'likely driven by systematic payroll-driven equity inflows.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 20
    # Calendar / payroll-cycle effect; documented across all market regimes.
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'pos_size_frac': 0.95,  # near-full allocation; scaled by regime
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if TICKER not in prices.columns:
            print(f'[{STRATEGY_ID}] SPY not in prices — returning []', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy = prices[TICKER].dropna()
        if len(spy) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        today_ts      = pd.Timestamp(spy.index[-1])
        prev_ts       = pd.Timestamp(spy.index[-2])
        today         = today_ts.date()
        prev_day      = prev_ts.date()

        payday      = _payday_date(today.year, today.month)
        prev_payday = _payday_date(prev_day.year, prev_day.month)

        current_price = float(spy.iloc[-1])
        scale         = self.position_scale(regime_state)
        pos_frac      = self.parameters.get('pos_size_frac', 0.95)

        stops = self.compute_stops_and_targets(
            spy,
            direction     = 'LONG',
            current_price = current_price,
            regime_state  = regime_state,
        )

        signals: List[Signal] = []

        if today == payday:
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
                    'calendar_event': 'payday_entry',
                    'payday':         str(payday),
                    'regime':         regime_state,
                },
            ))
        elif prev_day == prev_payday:
            # The previous trading day was the payday → exit today
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
                    'calendar_event': 'payday_exit',
                    'prev_payday':    str(prev_payday),
                    'regime':         regime_state,
                },
            ))

        print(
            f'[{STRATEGY_ID}] date={today} payday={payday} prev={prev_day} '
            f'signals={len(signals)}',
            file=sys.stderr,
        )
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


def _run_backtest() -> pd.DataFrame:
    """Simulate payday anomaly over full SPY history; return trades DataFrame."""
    root = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'master')
    spy = pd.read_parquet(os.path.join(root, 'prices.parquet'), columns=['SPY'])['SPY'].dropna().sort_index()
    regimes = pd.read_parquet(os.path.join(root, 'historical_regimes.parquet'))
    regime_col = 'state' if 'state' in regimes.columns else regimes.columns[0]
    regimes = regimes[[regime_col]].rename(columns={regime_col: 'regime_state'})

    rows = []
    dates = list(spy.index)
    for i, idx_date in enumerate(dates):
        ts = pd.Timestamp(idx_date)
        d  = ts.date()
        if d != _payday_date(d.year, d.month):
            continue   # not the payday for this month

        if i + 1 >= len(dates):
            continue   # no next bar available

        entry_price = float(spy.iloc[i])
        exit_price  = float(spy.iloc[i + 1])
        pnl         = (exit_price - entry_price) / entry_price

        # Regime at signal date
        regime_state = 'LOW_VOL'
        pos = regimes.index.searchsorted(idx_date)
        if pos > 0:
            regime_state = str(regimes.iloc[pos - 1]['regime_state'])

        rows.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  str(d),
            'regime_state': regime_state,
            'pnl':          float(pnl),
            'r_multiple':   float(pnl / 0.02),
        })

    return pd.DataFrame(rows)


if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition

    trades = _run_backtest()
    print(f'[PaydayAnomaly] {len(trades)} trades generated', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades,
        strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(result)
