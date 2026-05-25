"""
S_labor_day_week_momentum_reversal — Hanna (2025)
SPX momentum reversal during Labor Day week: a positive prior-month return
predicts weakness (SHORT SPY), while a negative prior-month return predicts
strength (LONG SPY).  Enter at close of the last trading day before Labor Day
Monday; exit at close +4 trading days (Fri of Labor Day week).
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from pathlib import Path
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['LaborDayWeekMomentumReversal']

STRATEGY_ID = 'S_labor_day_week_momentum_reversal'


def _labor_day(year: int) -> pd.Timestamp:
    """Return Labor Day (first Monday of September) for the given year."""
    sep1 = pd.Timestamp(year=year, month=9, day=1)
    days_to_monday = (7 - sep1.weekday()) % 7
    return sep1 + pd.Timedelta(days=days_to_monday)


class LaborDayWeekMomentumReversal(BaseStrategy):
    """Short SPY during Labor Day week after up month; Long after down month."""

    id                = STRATEGY_ID
    name              = 'LaborDayWeekMomentumReversal'
    description       = (
        'SPX momentum reversal during Labor Day week: short after positive '
        'prior-month return, long after negative prior-month return.'
    )
    tier              = 2
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {
            'hold_days':         4,
            'lookback_days':     21,
            'base_position_pct': 0.05,
        }

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
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

        if 'SPY' not in prices.columns:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy = prices['SPY'].dropna()
        lookback = self.parameters['lookback_days']
        if len(spy) < lookback + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        all_dates = sorted(prices.index)
        today_pos = len(all_dates) - 1
        if today_pos + 1 < len(all_dates):
            next_td = pd.Timestamp(all_dates[today_pos + 1])
        else:
            next_td = pd.Timestamp(all_dates[-1]) + pd.Timedelta(days=1)
            while next_td.weekday() >= 5:
                next_td += pd.Timedelta(days=1)

        # Only fire on the session whose next trading day is Labor Day
        labor_day = _labor_day(next_td.year)
        if next_td != labor_day:
            print('[debug] signals=0', file=sys.stderr)
            return []

        today_close = float(spy.iloc[-1])
        prior_close = float(spy.iloc[-(lookback + 1)])
        if prior_close <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        prior_month_ret = (today_close - prior_close) / prior_close
        direction = 'SHORT' if prior_month_ret > 0 else 'LONG'
        scale = self.position_scale(regime_state)
        st = self.compute_stops_and_targets(spy, direction, today_close, regime_state=regime_state)

        sig = Signal(
            ticker            = 'SPY',
            direction         = direction,
            entry_price       = round(today_close, 4),
            stop_loss         = st['stop'],
            target_1          = st['t1'],
            target_2          = st['t2'],
            target_3          = st['t3'],
            position_size_pct = round(self.parameters['base_position_pct'] * scale, 4),
            confidence        = 'MED',
            signal_params     = {
                'labor_day':       labor_day.strftime('%Y-%m-%d'),
                'prior_month_ret': round(prior_month_ret, 4),
                'hold_days':       self.parameters['hold_days'],
            },
        )
        print('[debug] signals=1', file=sys.stderr)
        return [sig]


# ─── Standalone backtest ───────────────────────────────────────────────────────

def _run_backtest() -> pd.DataFrame:
    root = Path(__file__).resolve().parents[3]
    spy = pd.read_parquet(
        root / 'data' / 'master' / 'prices.parquet',
        filters=[('ticker', '=', 'SPY')],
        columns=['date', 'close'],
    )
    spy['date'] = pd.to_datetime(spy['date'])
    spy = spy.sort_values('date').set_index('date')['close']

    reg_path = root / 'data' / 'master' / 'historical_regimes.parquet'
    if reg_path.exists():
        reg = pd.read_parquet(reg_path, columns=['date', 'regime'])
        reg['date'] = pd.to_datetime(reg['date'])
        reg = reg.set_index('date')['regime']
    else:
        reg = pd.Series(dtype=str)

    dates    = sorted(spy.index)
    date_idx = {d: i for i, d in enumerate(dates)}
    trades   = []

    for year in range(2000, 2026):
        ld = _labor_day(year)
        prev_days = [d for d in dates if d < ld]
        if not prev_days:
            continue
        entry_date = prev_days[-1]
        ei = date_idx[entry_date]
        if ei < 21 or ei + 4 >= len(dates):
            continue

        entry_close = float(spy.iloc[ei])
        prior_close = float(spy.iloc[ei - 21])
        if prior_close <= 0:
            continue

        prior_month_ret = (entry_close - prior_close) / prior_close
        direction = 'SHORT' if prior_month_ret > 0 else 'LONG'
        exit_close = float(spy.iloc[ei + 4])

        pnl = ((entry_close - exit_close) / entry_close
               if direction == 'SHORT'
               else (exit_close - entry_close) / entry_close)

        regime_state = str(reg.get(entry_date, 'LOW_VOL'))
        trades.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  entry_date.strftime('%Y-%m-%d'),
            'regime_state': regime_state,
            'pnl':          round(pnl, 6),
            'r_multiple':   round(pnl / 0.02, 4),
        })

    return pd.DataFrame(trades)


if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition
    trades_df = _run_backtest()
    print(f'Total qualifying trades: {len(trades_df)}')
    if not trades_df.empty:
        print(trades_df.to_string())
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(f"Eligible regimes proposed: {result['eligible_regimes_proposed']}")
    for r, s in result['regime_partition'].items():
        print(f"  {r}: {s}")
