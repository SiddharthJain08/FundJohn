"""S_visibility_graph_rsi — Backward-Visibility Graph RSI oscillator.

Rak (2026): price series backward-visibility graphs encode directional
persistence; VGRSI = 100 - 100/(1+rA) where rA weights amplitude and
frequency of geometrically visible price changes.
"""
from __future__ import annotations

import sys
import os
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['VisibilityGraphRSI']

STRATEGY_ID = 'S_visibility_graph_rsi'
WV          = 40    # backward-visibility window (bars)
THRESH_LO   = 30    # oversold → LONG crossover
THRESH_HI   = 70    # overbought → SHORT crossover
BASE_SIZE   = 0.04


def _compute_vgrsi(p: np.ndarray) -> float:
    """VGRSI for array of length WV+1 (index 0..WV-1 past, WV current)."""
    j = len(p) - 1
    pj = p[j]
    vis = []
    for i in range(1, j):
        pi = p[i]
        if i + 1 == j:
            vis.append(i)
            continue
        ks = np.arange(i + 1, j)
        line = pi + (pj - pi) * (ks - i) / (j - i)
        if np.all(p[ks] < line):
            vis.append(i)
    if not vis:
        return 50.0
    v = np.array(vis)
    chg = p[v] - p[v - 1]
    s_plus  = float(np.sum(np.maximum(chg, 0.0)))
    s_minus = float(np.sum(np.maximum(-chg, 0.0)))
    n_plus  = int(np.sum(chg > 0))
    n_minus = int(np.sum(chg < 0))
    if s_minus < 1e-10 or n_minus == 0:
        return 99.0
    ra = ((s_plus / s_minus) + (n_plus / n_minus)) / 2.0
    return float(100.0 - 100.0 / (1.0 + ra))


class VisibilityGraphRSI(BaseStrategy):
    """Backward-visibility graph RSI: LONG on oversold cross-up, SHORT on overbought cross-down."""

    id                = STRATEGY_ID
    name              = 'VisibilityGraphRSI'
    description       = ('Backward-visibility graph RSI oscillator generating '
                         'LONG/SHORT signals on geometric price structure crossovers.')
    tier              = 2
    min_lookback      = WV + 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        need = WV + 2
        if len(prices) < need:
            print(f'[debug] signals=0 (need {need} bars, got {len(prices)})', file=sys.stderr)
            return []
        scale = self.position_scale(regime_state)
        signals: List[Signal] = []
        tickers = [t for t in universe if t in prices.columns]
        for ticker in tickers:
            series = prices[ticker].dropna()
            if len(series) < need:
                continue
            arr = series.values
            vg_now  = _compute_vgrsi(arr[-(WV + 1):])
            vg_prev = _compute_vgrsi(arr[-(WV + 2):-1])
            direction = None
            if vg_prev <= THRESH_LO < vg_now:
                direction = 'LONG'
            elif vg_prev >= THRESH_HI > vg_now:
                direction = 'SHORT'
            if direction is None:
                continue
            cp = float(arr[-1])
            st = self.compute_stops_and_targets(
                series, direction, cp, regime_state=regime_state,
            )
            confidence = 'HIGH' if abs(vg_now - 50) > 25 else 'MED'
            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=cp,
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=float(min(BASE_SIZE * scale, 0.10)),
                confidence=confidence,
                signal_params={'vgrsi': round(vg_now, 2), 'vgrsi_prev': round(vg_prev, 2)},
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Regime-partitioned backtest ───────────────────────────────────────────────
if __name__ == '__main__':
    import json

    ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    try:
        long_df  = pd.read_parquet(os.path.join(ROOT, 'prices.parquet'))
        wide     = long_df.pivot_table(index='date', columns='ticker', values='close')
        wide.index = pd.to_datetime(wide.index)
        wide = wide.sort_index().loc['2017-01-01':'2025-12-31']

        reg_df = pd.read_parquet(os.path.join(ROOT, 'historical_regimes.parquet'))
        reg_df['date'] = pd.to_datetime(reg_df['date'])
        regime_map = dict(zip(reg_df['date'], reg_df['regime']))

        rows = []
        sample = [c for c in wide.columns if not c.startswith('^')][:60]
        for ticker in sample:
            s = wide[ticker].dropna()
            if len(s) < WV + 10:
                continue
            arr   = s.values
            dates = s.index
            for idx in range(WV + 2, len(arr) - 7):
                vg_now  = _compute_vgrsi(arr[idx - WV:idx + 1])
                vg_prev = _compute_vgrsi(arr[idx - WV - 1:idx])
                if vg_prev <= THRESH_LO < vg_now:
                    direction = 'LONG'
                elif vg_prev >= THRESH_HI > vg_now:
                    direction = 'SHORT'
                else:
                    continue
                entry  = arr[idx]
                exit_p = arr[idx + 7]
                raw_ret = (exit_p - entry) / entry if direction == 'LONG' else (entry - exit_p) / entry
                sig_date = dates[idx]
                rows.append({
                    'strategy_id': STRATEGY_ID,
                    'signal_date': str(sig_date.date()),
                    'regime_state': regime_map.get(sig_date, 'LOW_VOL'),
                    'pnl': float(raw_ret),
                    'r_multiple': float(raw_ret / 0.02),
                })

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
