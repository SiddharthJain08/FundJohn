"""S_kalman_laguerre_trend_crossover (variant 2) — Kalman/Laguerre edge-cross
with EMA trend filter.

Source: https://blog.quantish.io/2021/12/09/kalman-and-laguerre/ (quantish, 2021)

Thesis: same underlying paper as variant 1 — a Kalman filter, an Ehlers
Laguerre filter, and an EMA are combined to catch trend. The paper discloses
no parameters and no exact crossover mechanics, so this variant deliberately
diverges from a "3-way persistent stacking" reading on every axis:

  - Kalman filter: constant-VELOCITY (2-state, position+velocity) alpha-beta
    filter with fixed gains (alpha=0.35, beta=0.05) — a richer model than a
    plain scalar random-walk smoother, since a filter meant to lead price
    into a trend crossover plausibly needs a velocity term.
  - Laguerre filter: Ehlers 4-tap IIR with gamma=0.7 — the slower/smoother
    setting from Ehlers' original TASC article, on the theory that using the
    faster gamma=0.5 would make the Laguerre line behave almost identically
    to the Kalman line and defeat the point of blending two different
    smoothers.
  - EMA: span=20, used as a binary TREND FILTER (price above/below the EMA),
    not as a third rank in a three-way stack. "Crossovers between a Kalman
    filter, a Laguerre filter, and an EMA" is read here as: the Kalman/
    Laguerre pair generates the actual entry/exit trigger, while the EMA
    gates which crossovers are allowed to fire (only take the crossover if
    price already confirms the same direction against a slower EMA).
  - Crossover rule: EDGE-TRIGGERED. A signal fires only on the bar the
    Kalman line actually crosses the Laguerre line (not on every bar the
    ordering happens to hold) — the more literal reading of "crossover" as
    an event rather than a persistent state. LONG on Kalman crossing above
    Laguerre while price > EMA; exit to FLAT on Kalman crossing back below
    Laguerre (or price falling below EMA mid-trend). No SHORT leg — crypto
    shorting isn't available on Alpaca (instrument-class convention).
  - Regime eligibility: the source author attributes the reported edge to a
    single crypto BULL RUN — bull runs are typically HIGH realized-vol
    breakout regimes, not calm/contango conditions, so this variant is
    scoped to TRANSITIONING/HIGH_VOL rather than variant 1's LOW_VOL/
    TRANSITIONING pick.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal

__all__ = ['KalmanLaguerreTrendCrossoverTV2']

INSTRUMENT_CLASS = 'crypto'
STRATEGY_ID      = 'S_kalman_laguerre_trend_crossover'

BASKET          = ['BTC-USD', 'ETH-USD']
KALMAN_ALPHA    = 0.35
KALMAN_BETA     = 0.05
LAGUERRE_GAMMA  = 0.7
EMA_SPAN        = 20
BASE_SIZE       = 0.15
MIN_BARS        = 60


def _kalman_alphabeta(prices: np.ndarray, alpha: float = KALMAN_ALPHA,
                       beta: float = KALMAN_BETA) -> np.ndarray:
    """Constant-velocity (position + velocity) alpha-beta Kalman-style filter."""
    n = len(prices)
    x = np.zeros(n)   # smoothed level
    v = np.zeros(n)   # smoothed velocity
    x[0] = prices[0]
    v[0] = 0.0
    for k in range(1, n):
        x_pred = x[k - 1] + v[k - 1]
        residual = prices[k] - x_pred
        x[k] = x_pred + alpha * residual
        v[k] = v[k - 1] + beta * residual
    return x


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


class KalmanLaguerreTrendCrossoverTV2(BaseStrategy):
    """LONG BTC-USD/ETH-USD on a Kalman-crosses-above-Laguerre edge event,
    gated by price > EMA(20); exits to flat on the reverse cross or on price
    falling back below the EMA. No short leg."""

    id                = STRATEGY_ID
    name              = 'KalmanLaguerreTrendCrossoverTV2'
    description       = (
        'Kalman/Laguerre edge-triggered crossover gated by an EMA trend '
        'filter on BTC-USD and ETH-USD; LONG on cross-up, flat otherwise.'
    )
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = MIN_BARS
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['TRANSITIONING', 'HIGH_VOL']
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
            print(f'[{STRATEGY_ID}_tv2] no basket tickers in prices; signals=0', file=sys.stderr)
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
            kalman   = _kalman_alphabeta(arr, KALMAN_ALPHA, KALMAN_BETA)
            laguerre = _laguerre_filter(arr, gamma)
            ema      = series.ewm(span=ema_span, adjust=False).mean().values

            k_now, k_prev   = float(kalman[-1]), float(kalman[-2])
            l_now, l_prev   = float(laguerre[-1]), float(laguerre[-2])
            e_now           = float(ema[-1])
            price_now       = float(arr[-1])

            crossed_up = (k_prev <= l_prev) and (k_now > l_now)
            trend_ok   = price_now > e_now
            if not (crossed_up and trend_ok):
                continue

            if price_now <= 0:
                continue

            spread_pct = (k_now - l_now) / l_now if l_now > 0 else 0.0
            if spread_pct > 0.03:
                confidence = 'HIGH'
            elif spread_pct > 0.01:
                confidence = 'MED'
            else:
                confidence = 'LOW'

            pos_size = round((base_size / len(available)) * scale, 4)
            if pos_size < 0.001:
                continue

            st = self.compute_stops_and_targets(
                series, direction='LONG', current_price=price_now,
                regime_state=regime_state,
            )

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price_now,
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
                    'trigger':    'edge_cross',
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
            kalman   = _kalman_alphabeta(arr)
            laguerre = _laguerre_filter(arr)
            ema      = s.ewm(span=EMA_SPAN, adjust=False).mean().values

            in_position = False
            entry_idx   = None
            for idx in range(MIN_BARS, len(arr) - 1):
                crossed_up   = (kalman[idx - 1] <= laguerre[idx - 1]) and (kalman[idx] > laguerre[idx])
                crossed_down = (kalman[idx - 1] >= laguerre[idx - 1]) and (kalman[idx] < laguerre[idx])
                trend_ok     = arr[idx] > ema[idx]

                if not in_position and crossed_up and trend_ok:
                    in_position = True
                    entry_idx = idx
                elif in_position and (crossed_down or arr[idx] < ema[idx]):
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
