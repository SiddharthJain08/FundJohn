"""Precompute the per-strategy BACKTEST dashboard panel:
effective Sharpe, GBM-σ OUE counts (overall + per regime), and a
weekly-downsampled equity curve vs SP500 with per-point regime.

This is the dashboard's backtest panel — SEPARATE from the live
#trade-reports OUE digest. Persisted to strategy_backtest_panel.
"""
from __future__ import annotations
import json
import math
import os
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from execution.oue_classifier import classify          # noqa: E402
from strategies import historical_regimes               # noqa: E402

PRICES_PARQUET = ROOT / 'data' / 'master' / 'prices.parquet'


def classify_trades_oue(trades: list[dict],
                        hv21_for: Callable[[str, str], Optional[float]],
                        sigma_gate: float = 2.0) -> tuple[dict, dict]:
    """Classify each backtest trade Over/Under/Expected vs a zero-drift GBM
    expectation, reusing oue_classifier.classify (ev_gbm=0). Returns
    (overall_counts, by_regime_counts). Trades with no computable hv21 →
    'expected' (mirrors the live classifier's missing-EV fallback), so
    O+U+E == len(trades) always holds."""
    overall = {'over': 0, 'under': 0, 'expected': 0}
    by_regime: dict[str, dict] = {}
    for t in trades:
        regime = t.get('entry_regime') or 'UNKNOWN'
        slot = by_regime.setdefault(regime, {'over': 0, 'under': 0, 'expected': 0})
        hv = hv21_for(t['ticker'], str(t['entry_date']))
        if hv is None or not math.isfinite(hv) or hv <= 0:
            kind = 'expected'
        else:
            kind, _ = classify(float(t['pnl_pct']), int(t.get('holding_days') or 1),
                               ev_gbm=0.0, hv21=float(hv), sigma_gate=sigma_gate)
        overall[kind] += 1
        slot[kind] += 1
    return overall, by_regime


from backtest.unified_backtest import _portfolio_daily_returns   # noqa: E402


def effective_sharpe(total_sharpe: Optional[float], cadence_days: Optional[float]) -> Optional[float]:
    """Sharpe / sqrt(cadence). cadence floored at 1 day."""
    if total_sharpe is None:
        return None
    return float(total_sharpe) / math.sqrt(max(1.0, float(cadence_days or 1.0)))


def build_equity_curve(trades: list[dict],
                       bench_daily_ret: pd.Series,
                       regime_series_fn=historical_regimes.regime_series,
                       weekly: bool = True) -> list[dict]:
    """Reconstruct the strategy's equity curve from backtest trades (reusing
    unified_backtest._portfolio_daily_returns), overlay the benchmark
    (^GSPC) normalized to the same start (1.0), tag each point with its
    regime, and downsample to weekly. Returns [{date, strat_equity,
    spx_equity, regime}, ...] (ascending date)."""
    daily_ret, dates = _portfolio_daily_returns(trades)
    if len(daily_ret) == 0:
        return []
    idx = pd.DatetimeIndex(dates)
    strat_eq = pd.Series(np.cumprod(1.0 + daily_ret), index=idx)
    b = bench_daily_ret.reindex(pd.date_range(idx.min(), idx.max(), freq='D')).fillna(0.0)
    bench_eq_full = (1.0 + b).cumprod()
    bench_eq = bench_eq_full.reindex(idx, method='ffill')
    regimes = regime_series_fn(idx)
    regimes.index = idx
    frame = pd.DataFrame({'strat_equity': strat_eq,
                          'spx_equity': bench_eq.values,
                          'regime': regimes.values}, index=idx)
    if weekly:
        frame = frame.groupby(frame.index.to_period('W')).tail(1)
    # Normalize both equity series to 1.0 at the first sampled point.
    first_strat = float(frame['strat_equity'].iloc[0])
    first_bench = float(frame['spx_equity'].iloc[0])
    frame = frame.copy()
    frame['strat_equity'] = frame['strat_equity'] / first_strat
    frame['spx_equity'] = frame['spx_equity'] / first_bench
    out = []
    for ts, row in frame.iterrows():
        out.append({'date': ts.strftime('%Y-%m-%d'),
                    'strat_equity': round(float(row['strat_equity']), 6),
                    'spx_equity': round(float(row['spx_equity']), 6),
                    'regime': None if pd.isna(row['regime']) else str(row['regime'])})
    return out
