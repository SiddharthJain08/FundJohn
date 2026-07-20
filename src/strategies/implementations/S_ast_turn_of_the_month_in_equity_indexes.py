"""
Turn of the Month in Equity Indexes — ported from QuantConnect / Quantpedia.
Source: https://quantpedia.com/strategies/turn-of-the-month-in-equity-indexes/

Long SPY on the last trading day of each month; exit on the 3rd trading day of
the new month.  Position is flat all other days.
"""
from __future__ import annotations
import os, sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['AstTurnOfTheMonthInEquityIndexes']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID      = 'S_ast_turn_of_the_month_in_equity_indexes'
TICKER           = 'SPY'
HOLD_DAYS        = 3   # exit on close of this trading day of the new month


class AstTurnOfTheMonthInEquityIndexes(BaseStrategy):
    """Long SPY on the last trading day of each month; exit on the 3rd trading
    day of the new month (turn-of-month institutional cash-flow anomaly).
    Source: https://quantpedia.com/strategies/turn-of-the-month-in-equity-indexes/
    """

    id                = STRATEGY_ID
    name              = 'Turn of the Month in Equity Indexes'
    description       = (
        'Long SPY on the last trading day of each month; exit on the 3rd '
        'trading day of the new month (turn-of-month institutional flow anomaly).'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {'hold_days_new_month': HOLD_DAYS}

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
        if TICKER not in prices.columns or pd.isna(prices[TICKER].iloc[-1]):
            return []

        spy           = prices[TICKER]
        today         = prices.index[-1]
        current_price = float(spy.iloc[-1])
        scale         = self.position_scale(regime_state)
        hold_days     = int(self.parameters.get('hold_days_new_month', HOLD_DAYS))

        # Month-end: next business day is in a different calendar month
        next_bday    = today + pd.tseries.offsets.BDay(1)
        is_month_end = (next_bday.month != today.month)

        # Day-of-month count (1-based) using actual trading days in prices index
        this_key   = (today.year, today.month)
        month_list = sorted(dt for dt in prices.index if dt.year == today.year and dt.month == today.month)
        day_of_month = (month_list.index(today) + 1) if today in month_list else -1

        signals: List[Signal] = []

        if is_month_end:
            # Entry: last TD of month → go LONG
            st = self.compute_stops_and_targets(spy, 'LONG', current_price, regime_state=regime_state)
            signals.append(Signal(
                ticker=TICKER, direction='LONG', entry_price=current_price,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round(1.0 * scale, 4), confidence='HIGH',
                signal_params={'trigger': 'month_end_entry', 'hold_days': hold_days,
                               'regime': regime_state, 'scale': scale},
            ))
        elif 1 <= day_of_month < hold_days:
            # Hold window: days 1 … hold_days-1 of new month
            st = self.compute_stops_and_targets(spy, 'LONG', current_price, regime_state=regime_state)
            signals.append(Signal(
                ticker=TICKER, direction='LONG', entry_price=current_price,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round(1.0 * scale, 4), confidence='MED',
                signal_params={'trigger': 'tom_hold', 'day_of_month': day_of_month,
                               'hold_days': hold_days, 'regime': regime_state, 'scale': scale},
            ))
        elif day_of_month == hold_days:
            # Exit: 3rd trading day of new month → FLAT
            signals.append(Signal(
                ticker=TICKER, direction='FLAT', entry_price=current_price,
                stop_loss=round(current_price * 0.95, 4),
                target_1=current_price, target_2=current_price, target_3=current_price,
                position_size_pct=0.0, confidence='HIGH',
                signal_params={'trigger': 'tom_exit', 'day_of_month': day_of_month,
                               'hold_days': hold_days, 'regime': regime_state},
            ))

        print(
            f'[{STRATEGY_ID}] today={today.date()} dom={day_of_month} '
            f'is_month_end={is_month_end} signals={len(signals)} regime={regime_state}',
            file=sys.stderr,
        )
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition
    import json as _json

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    prices_df  = pd.read_parquet(f'{PARQUET_ROOT}/prices.parquet', columns=[TICKER])
    reg_df     = pd.read_parquet(f'{PARQUET_ROOT}/historical_regimes.parquet')
    prices_df.index = pd.to_datetime(prices_df.index)
    reg_df.index    = pd.to_datetime(reg_df.index)

    all_tds    = sorted(prices_df.index.tolist())
    month_tds: dict = {}
    for dt in all_tds:
        k = (dt.year, dt.month)
        month_tds.setdefault(k, []).append(dt)

    rows = []
    for dt in all_tds:
        # Is dt the last trading day of its month?
        nxt = dt + pd.tseries.offsets.BDay(1)
        if nxt.month == dt.month:
            continue
        entry_p = prices_df[TICKER].get(dt)
        if entry_p is None or pd.isna(entry_p):
            continue
        nxt_key  = (nxt.year, nxt.month)
        nxt_tds  = month_tds.get(nxt_key, [])
        if len(nxt_tds) < HOLD_DAYS:
            continue
        exit_dt  = nxt_tds[HOLD_DAYS - 1]
        exit_p   = prices_df[TICKER].get(exit_dt)
        if exit_p is None or pd.isna(exit_p):
            continue
        pnl    = (float(exit_p) - float(entry_p)) / float(entry_p)
        regime_row  = reg_df[reg_df.index <= dt].iloc[-1] if not reg_df.empty else None
        rstate = str(regime_row['state']) if (regime_row is not None and 'state' in reg_df.columns) else 'LOW_VOL'
        rows.append({'strategy_id': STRATEGY_ID, 'signal_date': dt, 'regime_state': rstate,
                     'pnl': pnl, 'r_multiple': round(pnl / 0.02, 4)})

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
