"""S_kalman_laguerre_trend_crossover — Kalman / Laguerre / EMA trend alignment.

Source: https://blog.quantish.io/2021/12/09/kalman-and-laguerre/ (quantish, 2021)

Thesis: three smoothed representations of price — a Kalman filter, an Ehlers
Laguerre filter, and an EMA — each react to trend at a different speed. When
the fast line (Kalman) sits above the medium line (Laguerre) which sits above
the slow line (EMA), the market is in a confirmed uptrend; when that ordering
breaks, the trend edge is gone. The source author explicitly cautions the
reported 3.58 Sharpe was measured during a single crypto bull run and is
likely a regime artifact, not skill — we treat this as a trend-following
sleeve that should only fire in calm/transitional regimes, never as a
crisis-alpha claim.

Interpretation variant (paper discloses no parameters or exact crossover
rule):
  - Kalman filter: scalar random-walk model (no velocity state), process
    variance 0.01 / measurement variance 0.10 — simpler than a constant-
    velocity alpha-beta filter a different implementation might choose.
  - Laguerre filter: Ehlers 4-tap IIR with gamma=0.5 (faster/more reactive
    than the gamma=0.8 commonly used in Ehlers' original TASC article).
  - EMA: span=34 (Fibonacci-adjacent, slower than the more common 20).
  - Crossover rule: full stacking alignment (kalman > laguerre > ema) is
    required to be LONG, evaluated fresh each cycle (persistent trend-state,
    not a one-bar crossover-day-only trigger). Alignment breaking exits to
    flat. No SHORT leg — crypto shorting isn't available on Alpaca, matching
    the instrument-class convention used elsewhere in this repo.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['KalmanLaguerreTrendCrossover']

INSTRUMENT_CLASS = 'crypto'
STRATEGY_ID      = 'S_kalman_laguerre_trend_crossover'

BASKET               = ['BTC-USD', 'ETH-USD']
KALMAN_PROCESS_VAR   = 0.01
KALMAN_MEASUREMENT_VAR = 0.10
LAGUERRE_GAMMA       = 0.5
EMA_SPAN             = 34
BASE_SIZE            = 0.15
MIN_BARS             = 60


def _kalman_filter(prices: np.ndarray, process_var: float = KALMAN_PROCESS_VAR,
                    measurement_var: float = KALMAN_MEASUREMENT_VAR) -> np.ndarray:
    """Scalar random-walk Kalman smoother (no velocity state)."""
    n = len(prices)
    xhat = np.zeros(n)
    p    = np.zeros(n)
    xhat[0] = prices[0]
    p[0]    = 1.0
    for k in range(1, n):
        xhat_minus = xhat[k - 1]
        p_minus    = p[k - 1] + process_var
        gain       = p_minus / (p_minus + measurement_var)
        xhat[k]    = xhat_minus + gain * (prices[k] - xhat_minus)
        p[k]       = (1.0 - gain) * p_minus
    return xhat


def _laguerre_filter(prices: np.ndarray, gamma: float = LAGUERRE_GAMMA) -> np.ndarray:
    """Ehlers 4-tap Laguerre filter."""
    n = len(prices)
    out = np.zeros(n)
    l0 = l1 = l2 = l3 = 0.0
    for i in range(n):
        l0_prev, l1_prev, l2_prev, l3_prev = l0, l1, l2, l3
        l0 = (1.0 - gamma) * prices[i] + gamma * l0_prev
        l1 = -gamma * l0 + l0_prev + gamma * l1_prev
        l2 = -gamma * l1 + l1_prev + gamma * l2_prev
        l3 = -gamma * l2 + l2_prev + gamma * l3_prev
        out[i] = (l0 + 2.0 * l1 + 2.0 * l2 + l3) / 6.0
    return out


class KalmanLaguerreTrendCrossover(BaseStrategy):
    """LONG BTC-USD/ETH-USD when Kalman > Laguerre > EMA (confirmed uptrend
    alignment); flat when the ordering breaks. No short leg."""

    id                = STRATEGY_ID
    name              = 'KalmanLaguerreTrendCrossover'
    description       = (
        'Kalman/Laguerre/EMA trend-alignment crossover on BTC-USD and '
        'ETH-USD; LONG on confirmed fast>medium>slow stacking, flat otherwise.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = MIN_BARS
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = len(BASKET)

    def default_parameters(self) -> dict:
        return {
            'gamma':    LAGUERRE_GAMMA,
            'ema_span': EMA_SPAN,
            'base_size': BASE_SIZE,
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

        available = [t for t in BASKET if t in prices.columns]
        if not available:
            print(f'[{STRATEGY_ID}] no basket tickers in prices; signals=0', file=sys.stderr)
            return []

        gamma     = float(self.parameters.get('gamma', LAGUERRE_GAMMA))
        ema_span  = int(self.parameters.get('ema_span', EMA_SPAN))
        base_size = float(self.parameters.get('base_size', BASE_SIZE))
        scale     = self.position_scale(regime_state)
        signals: List[Signal] = []

        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < MIN_BARS:
                continue

            arr = series.values.astype(float)
            kalman   = _kalman_filter(arr, KALMAN_PROCESS_VAR, KALMAN_MEASUREMENT_VAR)
            laguerre = _laguerre_filter(arr, gamma)
            ema      = series.ewm(span=ema_span, adjust=False).mean().values

            k_now, l_now, e_now = float(kalman[-1]), float(laguerre[-1]), float(ema[-1])

            aligned_long = k_now > l_now > e_now
            if not aligned_long:
                continue

            current_price = float(arr[-1])
            if current_price <= 0:
                continue

            spread_pct = (k_now - e_now) / e_now if e_now > 0 else 0.0
            if spread_pct > 0.05:
                confidence = 'HIGH'
            elif spread_pct > 0.02:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            pos_size = round((base_size / len(available)) * scale, 4)
            if pos_size < 0.001:
                continue

            st = self.compute_stops_and_targets(
                series, direction='LONG', current_price=current_price,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = current_price,
                stop_loss         = float(st['stop']),
                target_1          = float(st['t1']),
                target_2          = float(st['t2']),
                target_3          = float(st['t3']),
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'kalman':     round(k_now, 4),
                    'laguerre':   round(l_now, 4),
                    'ema':        round(e_now, 4),
                    'spread_pct': round(spread_pct, 4),
                    'gamma':      gamma,
                    'ema_span':   ema_span,
                    'regime':     regime_state,
                },
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Regime-partitioned backtest ───────────────────────────────────────────────
if __name__ == '__main__':
    import json
    import os

    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    try:
        long_df = pd.read_parquet(os.path.join(ROOT, 'prices.parquet'))
        wide    = long_df.pivot_table(index='date', columns='ticker', values='close')
        wide.index = pd.to_datetime(wide.index)
        wide = wide.sort_index().loc['2017-01-01':'2025-12-31']

        reg_df = pd.read_parquet(os.path.join(ROOT, 'historical_regimes.parquet'))
        reg_df['date'] = pd.to_datetime(reg_df['date'])
        regime_map = dict(zip(reg_df['date'], reg_df['regime']))

        rows = []
        for ticker in [t for t in BASKET if t in wide.columns]:
            s = wide[ticker].dropna()
            if len(s) < MIN_BARS + 10:
                continue
            arr   = s.values.astype(float)
            dates = s.index
            kalman   = _kalman_filter(arr)
            laguerre = _laguerre_filter(arr)
            ema      = s.ewm(span=EMA_SPAN, adjust=False).mean().values

            in_position = False
            entry_idx   = None
            for idx in range(MIN_BARS, len(arr) - 1):
                aligned = kalman[idx] > laguerre[idx] > ema[idx]
                if not in_position and aligned:
                    in_position = True
                    entry_idx = idx
                elif in_position and not aligned:
                    entry_price = arr[entry_idx]
                    exit_price  = arr[idx]
                    raw_ret = (exit_price - entry_price) / entry_price
                    sig_date = dates[entry_idx]
                    rows.append({
                        'strategy_id':  STRATEGY_ID,
                        'signal_date':  str(sig_date.date()),
                        'regime_state': regime_map.get(sig_date, 'LOW_VOL'),
                        'pnl':          float(raw_ret),
                        'r_multiple':   float(raw_ret / 0.05),
                    })
                    in_position = False

        trades_df = pd.DataFrame(rows)
        print(f'[backtest] total trades: {len(trades_df)}', file=sys.stderr)

        sys.path.insert(0, '/root/openclaw/src')
        from backtest.quick_backtest import run_backtest_with_regime_partition
        result = run_backtest_with_regime_partition(
            trades_df,
            strategy_id=STRATEGY_ID,
            thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
        )
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'[backtest] error: {e}', file=sys.stderr)
        sys.exit(1)
