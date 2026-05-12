from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['TransitioningOverboughtRevert']


class TransitioningOverboughtRevert(BaseStrategy):
    """SHORT overbought (RSI>70) equities exclusively in TRANSITIONING regime."""

    id          = 'S_transitioning_overbought_revert'
    name        = 'TransitioningOverboughtRevert'
    description = 'SHORT overbought RSI equities in TRANSITIONING regime — momentum unwind play'
    tier        = 2
    min_lookback = 20

    active_in_regimes = ['TRANSITIONING']

    def default_parameters(self) -> dict:
        return {
            'rsi_period':      14,
            'rsi_overbought':  70,
            'rsi_exit':        50,
            'max_signals':     20,
            'base_size':       0.03,
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
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        scale        = self.position_scale(regime_state)
        rsi_period   = int(self.parameters.get('rsi_period',     14))
        rsi_ob       = float(self.parameters.get('rsi_overbought', 70))
        base_size    = float(self.parameters.get('base_size',      0.03))
        max_sigs     = int(self.parameters.get('max_signals',     20))

        # Filter to universe tickers present in prices
        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Need at least rsi_period + 1 rows
        if len(prices) < rsi_period + 1:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        signals: List[Signal] = []

        for ticker in tickers:
            series = prices[ticker].dropna()
            if len(series) < rsi_period + 1:
                continue

            rsi = self._rsi(series, rsi_period)
            if rsi is None or np.isnan(rsi):
                continue

            if rsi <= rsi_ob:
                continue

            # Overbought in TRANSITIONING — SHORT signal
            current_price = float(series.iloc[-1])
            if current_price <= 0:
                continue

            stops = self.compute_stops_and_targets(
                series, 'SHORT', current_price, regime_state=regime_state
            )

            # Rank by RSI extremity — higher RSI gets slightly higher confidence
            if rsi >= 80:
                confidence = 'HIGH'
            elif rsi >= 75:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'SHORT',
                entry_price       = round(current_price, 4),
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(base_size * scale, 6),
                confidence        = confidence,
                signal_params     = {
                    'rsi_14':          round(rsi, 2),
                    'rsi_exit_thresh': rsi_ob,
                    'max_hold_days':   10,
                },
            ))

        # Sort by RSI descending (most overbought first), cap
        signals.sort(key=lambda s: s.signal_params.get('rsi_14', 0), reverse=True)
        signals = signals[:min(max_sigs, self.MAX_SIGNALS)]

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    @staticmethod
    def _rsi(series: pd.Series, period: int) -> float | None:
        """Wilder RSI — returns scalar for the last bar."""
        delta = series.diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        last_loss = float(avg_loss.iloc[-1])
        if last_loss == 0:
            return 100.0
        rs  = float(avg_gain.iloc[-1]) / last_loss
        return 100.0 - (100.0 / (1.0 + rs))


if __name__ == '__main__':
    import os
    import json
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    from backtest.quick_backtest import run_backtest_with_regime_partition

    STRATEGY_ID = 'S_transitioning_overbought_revert'
    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')

    prices_long = pd.read_parquet(os.path.join(ROOT, 'prices.parquet'))
    wide = prices_long.pivot_table(index='date', columns='ticker', values='close')
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()

    regimes = pd.read_parquet(os.path.join(ROOT, 'historical_regimes.parquet'))
    regimes['date'] = pd.to_datetime(regimes['date'])
    regime_map = dict(zip(regimes['date'], regimes['regime']))

    def _compute_rsi_df(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        delta = df.diff()
        gain  = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        loss  = (-delta).clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        return 100.0 - (100.0 / (1.0 + gain / loss.replace(0, np.nan)))

    rsi_df = _compute_rsi_df(wide)
    trans_dates = set(regimes.loc[regimes['regime'] == 'TRANSITIONING', 'date'])
    trans_mask = wide.index.isin(trans_dates)
    # NaN comparisons yield False, so signal_mask is bool-only
    signal_mask = (rsi_df > 70).loc[trans_mask]

    # 10-day fixed hold SHORT return (vectorized; no early RSI-exit in backtest path)
    fwd_ret = -(wide.pct_change(10).shift(-10))

    records = []
    for dt, row in signal_mask.iterrows():
        for ticker in row.index[row]:
            if ticker not in fwd_ret.columns:
                continue
            val = fwd_ret.at[dt, ticker]
            if pd.isna(val):
                continue
            pnl = float(val)
            records.append({
                'strategy_id': STRATEGY_ID,
                'signal_date': dt.strftime('%Y-%m-%d'),
                'regime_state': regime_map.get(dt, 'UNKNOWN'),
                'pnl': pnl,
                'r_multiple': pnl / 0.03,
            })

    trades_df = (pd.DataFrame(records) if records else
                 pd.DataFrame(columns=['strategy_id', 'signal_date',
                                       'regime_state', 'pnl', 'r_multiple']))
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
