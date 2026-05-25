"""
S_spx_death_cross_contrarian_fade — Hanna (2025)
When the S&P 500 50-day SMA crosses below the 200-day SMA (Death Cross),
enter LONG SPY as a contrarian fade of the bearish narrative.  Exit on
Golden Cross or hard -20% stop.  97-year study (1928-2025) shows 73.5%
win rate across 49 instances despite the dominant bearish lore.
"""
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from pathlib import Path
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['SpxDeathCrossContrarianFade']

STRATEGY_ID = 'S_spx_death_cross_contrarian_fade'


class SpxDeathCrossContrarianFade(BaseStrategy):
    """Long SPY on Death Cross (50d crosses below 200d SMA); exit at Golden Cross or -20% stop."""

    id                = STRATEGY_ID
    name              = 'SpxDeathCrossContrarianFade'
    description       = (
        'Contrarian long SPY when 50-day SMA crosses below 200-day SMA; '
        'exit at Golden Cross or -20% hard stop. 73.5% win rate over 97 years.'
    )
    tier              = 2
    min_lookback      = 504
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {
            'fast_ma':           50,
            'slow_ma':           200,
            'stop_loss_pct':     0.20,   # 20% hard stop — tail guard per spec
            'base_position_pct': 0.05,   # 5% of portfolio
        }

    def generate_signals(
        self,
        prices:    pd.DataFrame,
        regime:    dict,
        universe:  List[str],
        aux_data:  dict = None,
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

        spy  = prices['SPY'].dropna()
        fast = self.parameters['fast_ma']
        slow = self.parameters['slow_ma']

        if len(spy) < slow + 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        sma_fast = spy.rolling(fast).mean()
        sma_slow = spy.rolling(slow).mean()

        today_fast = float(sma_fast.iloc[-1])
        today_slow = float(sma_slow.iloc[-1])
        yest_fast  = float(sma_fast.iloc[-2])
        yest_slow  = float(sma_slow.iloc[-2])

        if any(pd.isna(v) for v in [today_fast, today_slow, yest_fast, yest_slow]):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Death Cross: fast crosses below slow
        if not (yest_fast >= yest_slow and today_fast < today_slow):
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale         = self.position_scale(regime_state)
        current_price = float(spy.iloc[-1])
        hard_stop     = round(current_price * (1.0 - self.parameters['stop_loss_pct']), 4)
        st            = self.compute_stops_and_targets(spy, 'LONG', current_price,
                                                       regime_state=regime_state)
        # Take the larger (less tight) of ATR-stop vs hard 20% stop
        effective_stop = max(st['stop'], hard_stop)

        sig = Signal(
            ticker            = 'SPY',
            direction         = 'LONG',
            entry_price       = round(current_price, 4),
            stop_loss         = round(effective_stop, 4),
            target_1          = st['t1'],
            target_2          = st['t2'],
            target_3          = st['t3'],
            position_size_pct = round(self.parameters['base_position_pct'] * scale, 4),
            confidence        = 'MED',
            signal_params     = {
                'event':         'death_cross',
                'sma_fast':      round(today_fast, 4),
                'sma_slow':      round(today_slow, 4),
                'stop_loss_pct': self.parameters['stop_loss_pct'],
                'exit_trigger':  'golden_cross_or_stop',
            },
        )
        print('[debug] signals=1', file=sys.stderr)
        return [sig]


# ─── Standalone backtest ──────────────────────────────────────────────────────

def _run_backtest() -> pd.DataFrame:
    """
    Simulate entries on Death Cross, exits on Golden Cross or -20% stop.
    Uses daily SPY close from data/master/prices.parquet.
    """
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

    sma50  = spy.rolling(50).mean()
    sma200 = spy.rolling(200).mean()

    trades    = []
    in_trade  = False
    entry_px  = None
    entry_dt  = None

    dates = spy.index.tolist()
    for i in range(201, len(dates)):
        d         = dates[i]
        prev_d    = dates[i - 1]
        close_now = float(spy.iloc[i])
        f_now     = float(sma50.iloc[i])
        s_now     = float(sma200.iloc[i])
        f_prev    = float(sma50.iloc[i - 1])
        s_prev    = float(sma200.iloc[i - 1])

        if pd.isna(f_now) or pd.isna(s_now) or pd.isna(f_prev) or pd.isna(s_prev):
            continue

        if not in_trade:
            # Death Cross entry
            if f_prev >= s_prev and f_now < s_now:
                in_trade = True
                entry_px = close_now
                entry_dt = d
        else:
            # Exit: Golden Cross or -20% stop
            stop_px   = entry_px * 0.80
            golden    = (f_prev < s_prev and f_now >= s_now)
            stopped   = close_now <= stop_px

            if golden or stopped:
                pnl          = (close_now - entry_px) / entry_px
                regime_state = str(reg.get(entry_dt, 'TRANSITIONING'))
                trades.append({
                    'strategy_id':  STRATEGY_ID,
                    'signal_date':  entry_dt.strftime('%Y-%m-%d'),
                    'regime_state': regime_state,
                    'pnl':          round(pnl, 6),
                    'r_multiple':   round(pnl / 0.20, 4),   # risk unit = 20% stop
                    'exit_reason':  'golden_cross' if golden else 'stop',
                })
                in_trade = False
                entry_px = None
                entry_dt = None

    return pd.DataFrame(trades)


if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition
    trades_df = _run_backtest()
    print(f'Total trades: {len(trades_df)}')
    if not trades_df.empty:
        print(trades_df.to_string())
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 5, 'min_avg_r': 0.0},
    )
    print(f"Eligible regimes proposed: {result['eligible_regimes_proposed']}")
    for r, s in result['regime_partition'].items():
        print(f'  {r}: {s}')
