from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.regime_performance_analyzer import (  # noqa: E402
    analyze_dataframe, compute_regime_stats, propose_eligible_regimes,
)

def _trades(rows):
    return pd.DataFrame(rows, columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])

def test_compute_regime_stats_sharpe_winrate():
    df = _trades([
        ('S1', '2026-01-01', 'LOW_VOL',  100, 1.5),
        ('S1', '2026-01-02', 'LOW_VOL',  -50, -0.5),
        ('S1', '2026-01-03', 'LOW_VOL',  120, 1.8),
        ('S1', '2026-01-04', 'LOW_VOL',   80, 1.2),
    ])
    stats = compute_regime_stats(df, 'S1', 'LOW_VOL')
    assert stats['trade_count'] == 4
    assert stats['win_rate'] == 0.75
    assert stats['avg_r_multiple'] == pytest.approx(1.0, abs=0.01)
    assert stats['sharpe'] > 0

def test_compute_regime_stats_no_trades():
    stats = compute_regime_stats(_trades([]), 'S1', 'LOW_VOL')
    assert stats['trade_count'] == 0
    assert stats['sharpe'] == 0.0

def test_propose_eligible_regimes_passes_thresholds():
    # Varied r_multiple to generate std > 0 and measurable Sharpe > 0.5.
    df = _trades([
        ('S1', f'2026-01-{i:02d}', 'LOW_VOL', 100 if i % 2 == 0 else 150, 1.2 if i % 2 == 0 else 1.8)
        for i in range(1, 25)
    ])
    thresholds = {'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0}
    eligible = propose_eligible_regimes(df, 'S1', thresholds)
    assert 'LOW_VOL' in eligible

def test_propose_eligible_regimes_below_trade_count():
    df = _trades([('S1', f'2026-01-{i:02d}', 'LOW_VOL', 100, 1.5) for i in range(1, 10)])
    thresholds = {'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0}
    eligible = propose_eligible_regimes(df, 'S1', thresholds)
    assert 'LOW_VOL' not in eligible

def test_propose_eligible_regimes_negative_avg_r():
    df = _trades([('S1', f'2026-01-{i:02d}', 'CRISIS', -100, -1.2) for i in range(1, 25)])
    thresholds = {'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0}
    eligible = propose_eligible_regimes(df, 'S1', thresholds)
    assert 'CRISIS' not in eligible

def test_analyze_dataframe_multi_regime():
    # LOW_VOL: varied r_multiple (std > 0, passes Sharpe threshold).
    # CRISIS: constant negative r_multiple (std == 0, Sharpe=0.0, fails threshold).
    df = _trades(
        [('S1', f'2026-01-{i:02d}', 'LOW_VOL', 100 if i % 2 == 0 else 150, 1.2 if i % 2 == 0 else 1.8)
         for i in range(1, 25)] +
        [('S1', f'2026-02-{i:02d}', 'CRISIS', -100, -1.2) for i in range(1, 25)]
    )
    thresholds = {'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0}
    result = analyze_dataframe(df, thresholds)
    assert result['S1']['eligible_regimes'] == ['LOW_VOL']
    assert result['S1']['stats']['LOW_VOL']['trade_count'] == 24
    assert result['S1']['stats']['CRISIS']['trade_count'] == 24
