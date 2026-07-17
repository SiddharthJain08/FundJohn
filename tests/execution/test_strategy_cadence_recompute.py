"""Tests for strategy_cadence_recompute worker.

Tests the pure function recompute_for_strategy() with various data scenarios,
exercising live history, static fallback, and bootstrap paths.
"""
from datetime import date, timedelta
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from execution.strategy_cadence_recompute import recompute_for_strategy


def test_recompute_with_live_history():
    """10 closed trades with synthesized exit dates → live_signal_pnl source."""
    base = date(2026, 1, 1)
    closed = [
        {
            'entry_ts': base + timedelta(days=i * 5),
            'exit_ts': base + timedelta(days=i * 5 + 2)
        }
        for i in range(10)
    ]  # avg 2 days hold
    result = recompute_for_strategy(
        'S1', last_fire=date(2026, 5, 11), closed_trades=closed
    )
    assert result['source'] == 'live_signal_pnl'
    assert result['avg_holding_days'] >= 2.0
    assert result['next_fire_date'] > date(2026, 5, 11)


def test_recompute_falls_back_to_static():
    """No trades, but strategy in static lookup → static_fallback source."""
    result = recompute_for_strategy(
        'S_static_known',
        last_fire=date(2026, 5, 11),
        closed_trades=[],
        static_lookup={'S_static_known': 3.0}
    )
    assert result['source'] == 'static_fallback'
    assert result['avg_holding_days'] == 3.0
    assert result['next_fire_date'] == date(2026, 5, 14)  # 5/11 + 3 days


def test_recompute_bootstraps_daily():
    """No trades and not in static lookup → bootstrap_daily source."""
    result = recompute_for_strategy(
        'S_unknown',
        last_fire=date(2026, 5, 11),
        closed_trades=[],
        static_lookup={}
    )
    assert result['source'] == 'bootstrap_daily'
    assert result['avg_holding_days'] == 1.0
    assert result['next_fire_date'] == date(2026, 5, 12)  # 5/11 + 1 day


def test_recompute_insufficient_trades_uses_static():
    """< 5 trades with valid exit dates → static_fallback if available."""
    base = date(2026, 1, 1)
    closed = [
        {
            'entry_ts': base,
            'exit_ts': base + timedelta(days=1)
        }
    ]  # only 1 trade
    result = recompute_for_strategy(
        'S2',
        last_fire=date(2026, 5, 11),
        closed_trades=closed,
        static_lookup={'S2': 2.5}
    )
    assert result['source'] == 'static_fallback'
    assert result['avg_holding_days'] == 2.5


def test_recompute_partial_exit_data():
    """Trades with missing entry_ts or exit_ts → skip and use fallback."""
    closed = [
        {'entry_ts': date(2026, 1, 1), 'exit_ts': date(2026, 1, 3)},
        {'entry_ts': date(2026, 1, 8), 'exit_ts': None},  # no exit
        {'entry_ts': None, 'exit_ts': date(2026, 1, 15)},  # no entry
    ]
    # Only 1 valid trade → < 5, fall back to static
    result = recompute_for_strategy(
        'S3',
        last_fire=date(2026, 5, 11),
        closed_trades=closed,
        static_lookup={'S3': 1.5}
    )
    assert result['source'] == 'static_fallback'
    assert result['avg_holding_days'] == 1.5


def test_recompute_next_fire_date_calculation():
    """Verify next_fire_date = ceil(avg_holding_days) from last_fire_date."""
    base = date(2026, 1, 1)
    # 5 trades with 2-day average hold (ceil = 2)
    closed = [
        {
            'entry_ts': base,
            'exit_ts': base + timedelta(days=2)
        },
        {
            'entry_ts': base + timedelta(days=3),
            'exit_ts': base + timedelta(days=5)
        },
        {
            'entry_ts': base + timedelta(days=6),
            'exit_ts': base + timedelta(days=8)
        },
        {
            'entry_ts': base + timedelta(days=9),
            'exit_ts': base + timedelta(days=11)
        },
        {
            'entry_ts': base + timedelta(days=12),
            'exit_ts': base + timedelta(days=14)
        },
    ]
    result = recompute_for_strategy(
        'S4', last_fire=date(2026, 5, 11), closed_trades=closed
    )
    assert result['source'] == 'live_signal_pnl'
    assert result['avg_holding_days'] == 2.0
    # ceil(2.0) = 2, so next_fire = 5/11 + 2 = 5/13
    assert result['next_fire_date'] == date(2026, 5, 13)
