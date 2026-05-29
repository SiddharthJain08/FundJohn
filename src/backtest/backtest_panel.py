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
