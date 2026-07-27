"""
Linear Regression Channel Trend — S_linreg_channel_trend

Always-in-market SPY channel strategy (Perry Kaufman / Volkova 2025).
Fits an OLS regression line over N bars, bounds it by the historical
max/min price deviations, then enters LONG near the lower band and
SHORT near the upper band within a configurable zone.

Reference: https://financial-hacker.com/trading-the-channel/ (2025)
Walk-forward reported: profit factor 7.0, win rate 76% on SPY.
"""
from __future__ import annotations

import os
import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['LinRegChannelTrend']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID = 'S_linreg_channel_trend'

TICKER = 'SPY'
DEFAULT_N = 40        # walk-forward optimal (range: 20–150)
DEFAULT_FACTOR = 0.20  # zone width fraction (range: 0.05–0.50)


def _compute_channel(close_arr: np.ndarray, n: int) -> tuple:
    """OLS regression line over last n bars → (linval, high_dev, low_dev).

    linval   — predicted price at the last bar (x = n-1)
    high_dev — max(close[i] - linreg[i]) over window  (upper deviation)
    low_dev  — max(linreg[i] - close[i]) over window  (lower deviation)
    """
    window = close_arr[-n:].astype(float)
    x = np.arange(n, dtype=float)
    xm, ym = x.mean(), window.mean()
    denom = ((x - xm) ** 2).sum()
    if denom == 0.0:
        slope, intercept = 0.0, ym
    else:
        slope = ((x - xm) * (window - ym)).sum() / denom
        intercept = ym - slope * xm
    linreg_line = slope * x + intercept
    linval = float(linreg_line[-1])
    high_dev = float(max((window - linreg_line).max(), 0.0))
    low_dev = float(max((linreg_line - window).max(), 0.0))
    return linval, high_dev, low_dev


class LinRegChannelTrend(BaseStrategy):
    """Dynamic linear regression channel mean-reversion on SPY.

    LONG when price falls into the lower zone (near channel bottom);
    SHORT when price rises into the upper zone (near channel top).
    No signal in the neutral mid-zone; existing position is held.
    """

    id                = STRATEGY_ID
    name              = 'Linear Regression Channel Trend'
    description       = (
        'Always-in-market SPY channel strategy: LONG near lower regression '
        'band, SHORT near upper band (N=40, Factor=0.20).'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = DEFAULT_N + 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {
            'n':             DEFAULT_N,
            'factor':        DEFAULT_FACTOR,
            'pos_size_frac': 0.10,
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
            print(f'[LinRegChannelTrend] {TICKER} not in prices', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        n = int(self.parameters.get('n', DEFAULT_N))
        factor = float(self.parameters.get('factor', DEFAULT_FACTOR))
        series = prices[TICKER].dropna()

        if len(series) < n + 1:
            print(f'[LinRegChannelTrend] {len(series)} rows < {n+1}', file=sys.stderr)
            print('[debug] signals=0', file=sys.stderr)
            return []

        linval, high_dev, low_dev = _compute_channel(series.values, n)
        current_price = float(series.iloc[-1])
        zone = factor * (high_dev + low_dev)

        long_threshold  = linval - low_dev + zone
        short_threshold = linval + high_dev - zone

        if current_price < long_threshold:
            direction = 'LONG'
            penetration = (long_threshold - current_price) / max(low_dev, 1e-9)
        elif current_price > short_threshold:
            direction = 'SHORT'
            penetration = (current_price - short_threshold) / max(high_dev, 1e-9)
        else:
            print('[debug] signals=0', file=sys.stderr)
            return []

        if penetration > 0.30:
            confidence = 'HIGH'
        elif penetration > 0.10:
            confidence = 'MED'
        else:
            confidence = 'LOW'

        scale = self.position_scale(regime_state)
        pos_size = round(float(self.parameters.get('pos_size_frac', 0.10)) * scale, 4)
        stops = self.compute_stops_and_targets(
            series, direction=direction,
            current_price=current_price, regime_state=regime_state,
        )

        print(
            f'[LinRegChannelTrend] dir={direction} px={current_price:.2f} '
            f'linval={linval:.2f} zone={zone:.2f} '
            f'L_thr={long_threshold:.2f} S_thr={short_threshold:.2f} '
            f'regime={regime_state} confidence={confidence}',
            file=sys.stderr,
        )
        print('[debug] signals=1', file=sys.stderr)

        return [Signal(
            ticker            = TICKER,
            direction         = direction,
            entry_price       = current_price,
            stop_loss         = stops['stop'],
            target_1          = stops['t1'],
            target_2          = stops['t2'],
            target_3          = stops['t3'],
            position_size_pct = pos_size,
            confidence        = confidence,
            signal_params     = {
                'linval':          round(linval, 4),
                'high_dev':        round(high_dev, 4),
                'low_dev':         round(low_dev, 4),
                'zone':            round(zone, 4),
                'long_threshold':  round(long_threshold, 4),
                'short_threshold': round(short_threshold, 4),
                'n':               n,
                'factor':          factor,
                'regime':          regime_state,
                'scale':           scale,
            },
        )]


# ---------------------------------------------------------------------------
# Regime-partitioned backtest (required by lifecycle promotion gate)
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    prices = pd.read_parquet(os.path.join(PARQUET_ROOT, 'prices.parquet'), columns=[TICKER])
    regimes = pd.read_parquet(os.path.join(PARQUET_ROOT, 'historical_regimes.parquet'))

    # Align regime series: index = date, value = regime_state string
    if 'date' in regimes.columns:
        regimes = regimes.set_index('date')
    regime_series = regimes['regime_state'] if 'regime_state' in regimes.columns else regimes.iloc[:, 0]

    n, factor = DEFAULT_N, DEFAULT_FACTOR
    close = prices[TICKER].dropna().sort_index()
    rows = []
    for i in range(n + 1, len(close)):
        window = close.iloc[:i]
        if len(window) < n + 1:
            continue
        linval, high_dev, low_dev = _compute_channel(window.values, n)
        px = float(window.iloc[-1])
        zone = factor * (high_dev + low_dev)
        long_thr  = linval - low_dev + zone
        short_thr = linval + high_dev - zone

        if px < long_thr:
            direction = 'LONG'
        elif px > short_thr:
            direction = 'SHORT'
        else:
            continue

        dt = window.index[-1]
        r_state = regime_series.get(dt, 'LOW_VOL') if hasattr(regime_series, 'get') else 'LOW_VOL'
        try:
            r_state = regime_series.loc[dt]
        except (KeyError, TypeError):
            r_state = 'LOW_VOL'

        if i + 1 < len(close):
            next_px = float(close.iloc[i])
            pnl = (next_px - px) if direction == 'LONG' else (px - next_px)
            r_multiple = pnl / max(px * 0.02, 1e-9)
        else:
            pnl, r_multiple = 0.0, 0.0

        rows.append({
            'strategy_id':  STRATEGY_ID,
            'signal_date':  str(dt.date() if hasattr(dt, 'date') else dt)[:10],
            'regime_state': str(r_state),
            'pnl':          float(pnl),
            'r_multiple':   float(r_multiple),
        })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] total trades: {len(trades_df)}', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    import json
    print(json.dumps(result, indent=2, default=str))
