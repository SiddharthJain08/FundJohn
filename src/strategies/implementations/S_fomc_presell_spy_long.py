"""
S_fomc_presell_spy_long — Hanna (2025)
LONG SPY at close of the trading day before an FOMC meeting when that session
closes down ≥1% in the bottom half of its intraday range.  Pre-Fed forced
selling reverses into relief-buying once rate-decision uncertainty resolves.
Hold for 3 trading days; exit at close.
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from pathlib import Path
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['FomcPresellSpyLong']

STRATEGY_ID = 'S_fomc_presell_spy_long'

# FOMC announcement dates (rate decision day), 2017-2026.
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
_FOMC_STRINGS = [
    '2017-02-01','2017-03-15','2017-05-03','2017-06-14','2017-07-26',
    '2017-09-20','2017-11-01','2017-12-13',
    '2018-01-31','2018-03-21','2018-05-02','2018-06-13','2018-08-01',
    '2018-09-26','2018-11-08','2018-12-19',
    '2019-01-30','2019-03-20','2019-05-01','2019-06-19','2019-07-31',
    '2019-09-18','2019-10-30','2019-12-11',
    '2020-01-29','2020-03-03','2020-03-15','2020-04-29','2020-06-10',
    '2020-07-29','2020-09-16','2020-11-05','2020-12-16',
    '2021-01-27','2021-03-17','2021-04-28','2021-06-16','2021-07-28',
    '2021-09-22','2021-11-03','2021-12-15',
    '2022-01-26','2022-03-16','2022-05-04','2022-06-15','2022-07-27',
    '2022-09-21','2022-11-02','2022-12-14',
    '2023-02-01','2023-03-22','2023-05-03','2023-06-14','2023-07-26',
    '2023-09-20','2023-11-01','2023-12-13',
    '2024-01-31','2024-03-20','2024-05-01','2024-06-12','2024-07-31',
    '2024-09-18','2024-11-07','2024-12-18',
    '2025-01-29','2025-03-19','2025-05-07','2025-06-18','2025-07-30',
    '2025-09-17','2025-10-29','2025-12-10',
    '2026-01-28','2026-03-18','2026-04-29','2026-06-10','2026-07-29',
    '2026-09-16','2026-10-28','2026-12-09',
]
_FOMC_DT = frozenset(pd.to_datetime(_FOMC_STRINGS))


class FomcPresellSpyLong(BaseStrategy):
    """Long SPY day-before-FOMC when SPY closes ≥1% down in bottom half of intraday range."""

    id               = STRATEGY_ID
    name             = 'FomcPresellSpyLong'
    description      = (
        'Long SPY at close of session before FOMC when SPY is ≥1% down '
        'and closing in the bottom half of its daily range; exit +3 trading days.'
    )
    tier             = 2
    min_lookback     = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            'hold_days':         3,
            'min_pct_change':   -0.01,   # must close <= -1%
            'base_position_pct': 0.06,   # 6% of portfolio
        }

    def _load_spy_ohlc(self) -> pd.DataFrame:
        try:
            root = Path(__file__).resolve().parents[3]
            path = root / 'data' / 'master' / 'prices.parquet'
            df = pd.read_parquet(path, filters=[('ticker', '=', 'SPY')],
                                 columns=['date', 'high', 'low', 'close'])
            df['date'] = pd.to_datetime(df['date'])
            return df.set_index('date').sort_index()
        except Exception:
            return pd.DataFrame()

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

        if 'SPY' not in prices.columns or len(prices) < 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        today = pd.Timestamp(prices.index[-1])

        # Is the NEXT trading session an FOMC day?
        all_dates = sorted(prices.index)
        today_pos = len(all_dates) - 1
        if today_pos + 1 < len(all_dates):
            next_td = pd.Timestamp(all_dates[today_pos + 1])
        else:
            # Advance calendar days skipping weekends as a fallback
            next_td = today + pd.Timedelta(days=1)
            while next_td.weekday() >= 5:
                next_td += pd.Timedelta(days=1)

        if next_td not in _FOMC_DT:
            print('[debug] signals=0', file=sys.stderr)
            return []

        spy = prices['SPY']
        today_close = float(spy.iloc[-1])
        prev_close  = float(spy.iloc[-2])
        if prev_close <= 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        pct_chg = (today_close - prev_close) / prev_close
        if pct_chg > self.parameters['min_pct_change']:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Intraday range filter — close must be in bottom half of day's H-L range
        ohlc = self._load_spy_ohlc()
        if not ohlc.empty and today in ohlc.index:
            row = ohlc.loc[today]
            midpoint = (float(row['high']) + float(row['low'])) / 2.0
            if today_close >= midpoint:
                print('[debug] signals=0', file=sys.stderr)
                return []

        scale = self.position_scale(regime_state)
        st = self.compute_stops_and_targets(spy, 'LONG', today_close, regime_state=regime_state)

        sig = Signal(
            ticker            = 'SPY',
            direction         = 'LONG',
            entry_price       = round(today_close, 4),
            stop_loss         = st['stop'],
            target_1          = st['t1'],
            target_2          = st['t2'],
            target_3          = st['t3'],
            position_size_pct = round(self.parameters['base_position_pct'] * scale, 4),
            confidence        = 'HIGH',
            signal_params     = {
                'fomc_date':  next_td.strftime('%Y-%m-%d'),
                'spy_pct_chg': round(pct_chg, 4),
                'hold_days':  self.parameters['hold_days'],
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
        columns=['date', 'high', 'low', 'close'],
    )
    spy['date'] = pd.to_datetime(spy['date'])
    spy = spy.sort_values('date').set_index('date')

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

    for fomc_date in sorted(_FOMC_DT):
        prev = [d for d in dates if d < fomc_date]
        if not prev:
            continue
        sd = prev[-1]
        si = date_idx[sd]
        if si == 0 or si + 3 >= len(dates):
            continue

        today_close = float(spy.loc[sd, 'close'])
        prev_close  = float(spy.iloc[si - 1]['close'])
        if prev_close <= 0:
            continue
        pct_chg = (today_close - prev_close) / prev_close
        if pct_chg > -0.01:
            continue
        midpoint = (float(spy.loc[sd, 'high']) + float(spy.loc[sd, 'low'])) / 2.0
        if today_close >= midpoint:
            continue

        exit_close = float(spy.iloc[si + 3]['close'])
        pnl = (exit_close - today_close) / today_close
        regime_state = str(reg.get(sd, 'LOW_VOL'))

        trades.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  sd.strftime('%Y-%m-%d'),
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
