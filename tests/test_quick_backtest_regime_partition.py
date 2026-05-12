from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pandas as pd
from backtest.quick_backtest import run_backtest_with_regime_partition

def test_run_backtest_returns_regime_partition():
    # Minimal synthetic: 4 trades, 2 in LOW_VOL, 2 in HIGH_VOL
    trades = pd.DataFrame([
        {'strategy_id': 'S1', 'signal_date': '2026-01-01', 'regime_state': 'LOW_VOL', 'pnl': 100, 'r_multiple': 1.5},
        {'strategy_id': 'S1', 'signal_date': '2026-01-02', 'regime_state': 'LOW_VOL', 'pnl': -50, 'r_multiple': -0.5},
        {'strategy_id': 'S1', 'signal_date': '2026-02-01', 'regime_state': 'HIGH_VOL', 'pnl': -80, 'r_multiple': -0.8},
        {'strategy_id': 'S1', 'signal_date': '2026-02-02', 'regime_state': 'HIGH_VOL', 'pnl': -60, 'r_multiple': -0.6},
    ])
    result = run_backtest_with_regime_partition(trades, strategy_id='S1', thresholds={
        'min_sharpe': 0.5, 'min_trade_count': 1, 'min_avg_r': 0.0,
    })
    assert 'regime_partition' in result
    assert 'eligible_regimes_proposed' in result
    assert result['regime_partition']['LOW_VOL']['trade_count'] == 2
    assert result['regime_partition']['HIGH_VOL']['trade_count'] == 2
