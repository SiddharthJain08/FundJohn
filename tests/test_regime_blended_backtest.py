from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.regime_blended_backtest import (  # noqa: E402
    compute_blended_pnl, compute_production_pnl, aggregate_metrics,
    fire_frequency_per_strategy, mode_distribution,
)


def _trades_df(rows):
    return pd.DataFrame(rows, columns=['strategy_id', 'signal_id', 'signal_date',
                                          'ticker', 'direction', 'regime_state',
                                          'pnl', 'r_multiple'])


def test_compute_production_pnl_passthrough():
    df = _trades_df([
        ('S1', 'x1', '2026-01-01', 'AAPL', 'LONG', 'LOW_VOL', 0.05, 0.05),
        ('S2', 'x2', '2026-01-02', 'AAPL', 'LONG', 'LOW_VOL', -0.03, -0.03),
    ])
    out = compute_production_pnl(df)
    assert len(out) == 2
    assert out['sizer'].unique().tolist() == ['production']


def test_compute_blended_pnl_filters_by_eligible_regimes():
    df = _trades_df([
        ('S1', 'x1', '2026-01-01', 'AAPL', 'LONG', 'LOW_VOL', 0.05, 0.05),
        ('S1', 'x2', '2026-02-01', 'AAPL', 'LONG', 'CRISIS', 0.10, 0.10),  # filtered
        ('S2', 'x3', '2026-01-03', 'AAPL', 'LONG', 'CRISIS', -0.02, -0.02),  # passes (no eligible_regimes)
    ])
    manifest = {'strategies': {
        'S1': {'eligible_regimes': ['LOW_VOL']},  # only LOW_VOL
        # S2: no eligible_regimes → backward-compat passes
    }}
    out = compute_blended_pnl(df, manifest)
    assert len(out) == 2  # x1 (S1+LOW_VOL) + x3 (S2 anywhere)
    dates = set(str(d) for d in out['signal_date'])
    assert dates == {'2026-01-01', '2026-01-03'}


def test_aggregate_metrics_empty():
    out = aggregate_metrics(pd.DataFrame(columns=['signal_date', 'sizer', 'pnl']))
    assert out['sharpe'] == 0.0
    assert out['trade_count'] == 0


def test_aggregate_metrics_basic():
    df = pd.DataFrame([
        {'signal_date': '2026-01-01', 'sizer': 'b', 'pnl': 0.05},
        {'signal_date': '2026-01-02', 'sizer': 'b', 'pnl': -0.03},
        {'signal_date': '2026-01-03', 'sizer': 'b', 'pnl': 0.04},
        {'signal_date': '2026-01-04', 'sizer': 'b', 'pnl': 0.02},
    ])
    m = aggregate_metrics(df)
    assert m['trade_count'] == 4
    assert m['total_return'] == pytest.approx(0.08, abs=1e-6)
    assert m['sharpe'] > 0  # 3 of 4 positive


def test_fire_frequency_per_strategy():
    df = _trades_df([
        ('S1', 'x1', '2026-01-01', 'AAPL', 'LONG', 'LOW_VOL', 0.05, 0.05),
        ('S1', 'x2', '2026-01-15', 'AAPL', 'LONG', 'LOW_VOL', 0.03, 0.03),
        ('S2', 'x3', '2026-01-20', 'AAPL', 'LONG', 'LOW_VOL', -0.01, -0.01),
    ])
    freq = fire_frequency_per_strategy(df)
    assert 'S1' in freq
    assert 'S2' in freq
    assert freq['S1'] == 2.0  # 2 trades in 1 month
    assert freq['S2'] == 1.0


def test_mode_distribution():
    df = _trades_df([
        ('S1', 'x1', '2026-01-01', 'AAPL', 'L', 'LOW_VOL', 0.05, 0.05),
        ('S1', 'x2', '2026-01-02', 'AAPL', 'L', 'LOW_VOL', 0.03, 0.03),
        ('S1', 'x3', '2026-01-03', 'AAPL', 'L', 'CRISIS', -0.01, -0.01),
    ])
    dist = mode_distribution(df)
    assert dist['LOW_VOL'] == pytest.approx(0.667, abs=0.01)
    assert dist['CRISIS'] == pytest.approx(0.333, abs=0.01)
