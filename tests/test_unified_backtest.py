"""Tests for src/backtest/unified_backtest.py.

Focused on the pure helpers (signal direction normalization, trade
simulation, metric aggregation, per-regime grouping). The full
end-to-end run hits parquet + Postgres + a real strategy class and
is exercised via the smoke run captured in CI separately.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402


class TestSignalDirection(unittest.TestCase):
    def test_long_variants(self):
        for tag in ('LONG', 'BUY', 'BUY_VOL', 'long'):
            self.assertEqual(ub._signal_to_long_short(tag), 1)

    def test_short_variants(self):
        for tag in ('SHORT', 'SELL', 'SELL_VOL', 'short'):
            self.assertEqual(ub._signal_to_long_short(tag), -1)

    def test_flat_returns_zero(self):
        self.assertEqual(ub._signal_to_long_short('FLAT'), 0)
        self.assertEqual(ub._signal_to_long_short(None), 0)
        self.assertEqual(ub._signal_to_long_short(''), 0)


def _bars(rows):
    """Build a small OHLC DataFrame indexed by date."""
    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['date'])
    return df.set_index('date')[['open','high','low','close']]


class TestSimulateTrade(unittest.TestCase):
    def test_long_target_hit(self):
        bars = _bars([
            {'date': '2024-01-02', 'open': 100, 'high': 102, 'low': 99,  'close': 101},
            {'date': '2024-01-03', 'open': 101, 'high': 115, 'low': 100, 'close': 113},  # target=110 hit
        ])
        out = ub.simulate_trade(bars, pd.Timestamp('2024-01-02'), direction=1,
                                 entry_price=100.0, stop_loss=95.0, target_1=110.0,
                                 max_hold_days=21)
        self.assertEqual(out['exit_reason'], 'target')
        self.assertAlmostEqual(out['exit_price'], 110.0)
        self.assertAlmostEqual(out['pnl_pct'], 0.10)
        self.assertEqual(out['holding_days'], 1)

    def test_long_stop_hit(self):
        bars = _bars([
            {'date': '2024-01-02', 'open': 100, 'high': 102, 'low': 99,  'close': 101},
            {'date': '2024-01-03', 'open': 100, 'high': 100, 'low': 90,  'close': 92},
        ])
        out = ub.simulate_trade(bars, pd.Timestamp('2024-01-02'), direction=1,
                                 entry_price=100.0, stop_loss=95.0, target_1=110.0,
                                 max_hold_days=21)
        self.assertEqual(out['exit_reason'], 'stop')
        self.assertAlmostEqual(out['pnl_pct'], -0.05)

    def test_short_target_hit(self):
        bars = _bars([
            {'date': '2024-01-02', 'open': 100, 'high': 100, 'low': 99,  'close': 99},
            {'date': '2024-01-03', 'open': 99,  'high': 100, 'low': 89,  'close': 90},  # target=90 hit
        ])
        out = ub.simulate_trade(bars, pd.Timestamp('2024-01-02'), direction=-1,
                                 entry_price=100.0, stop_loss=105.0, target_1=90.0,
                                 max_hold_days=21)
        self.assertEqual(out['exit_reason'], 'target')
        self.assertAlmostEqual(out['pnl_pct'], 0.10)

    def test_max_hold_exit(self):
        # Bracket never fires; flat market for 21 bars.
        rows = [{'date': f'2024-01-{d:02d}', 'open': 100, 'high': 101, 'low': 99, 'close': 100}
                for d in range(2, 25)]
        bars = _bars(rows)
        out = ub.simulate_trade(bars, pd.Timestamp('2024-01-02'), direction=1,
                                 entry_price=100.0, stop_loss=80.0, target_1=120.0,
                                 max_hold_days=5)
        self.assertEqual(out['exit_reason'], 'max_hold')
        self.assertEqual(out['holding_days'], 5)
        self.assertAlmostEqual(out['exit_price'], 100.0)

    def test_end_of_data_exit(self):
        # Only one future bar available — strategy can't max-hold its window.
        bars = _bars([
            {'date': '2024-01-02', 'open': 100, 'high': 100, 'low': 99,  'close': 100},
            {'date': '2024-01-03', 'open': 100, 'high': 102, 'low': 99,  'close': 101},
        ])
        out = ub.simulate_trade(bars, pd.Timestamp('2024-01-02'), direction=1,
                                 entry_price=100.0, stop_loss=80.0, target_1=120.0,
                                 max_hold_days=21)
        self.assertEqual(out['exit_reason'], 'end_of_data')


class TestAggregateMetrics(unittest.TestCase):
    def test_empty_trades(self):
        m = ub.aggregate_metrics([])
        self.assertEqual(m['total_trades'], 0)
        self.assertIsNone(m['sharpe'])

    def test_consistent_winner(self):
        trades = [{'pnl_pct': 0.05, 'holding_days': 5}] * 100
        m = ub.aggregate_metrics(trades)
        self.assertEqual(m['total_trades'], 100)
        self.assertGreater(m['return_pct'], 0)
        # Zero variance → sharpe None
        self.assertIsNone(m['sharpe'])
        self.assertEqual(m['hit_rate'], 1.0)

    def test_hit_rate_and_drawdown(self):
        # 6 wins of +10%, 4 losses of -5%; expected hit_rate = 0.6
        trades = ([{'pnl_pct': 0.10, 'holding_days': 5}] * 6 +
                  [{'pnl_pct': -0.05, 'holding_days': 5}] * 4)
        m = ub.aggregate_metrics(trades)
        self.assertAlmostEqual(m['hit_rate'], 0.6)
        self.assertGreater(m['return_pct'], 0)


class TestAggregatePerRegime(unittest.TestCase):
    def test_groups_trades_by_entry_regime(self):
        trades = [
            {'pnl_pct': 0.05, 'holding_days': 5, 'entry_regime': 'LOW_VOL'},
            {'pnl_pct': 0.10, 'holding_days': 5, 'entry_regime': 'LOW_VOL'},
            {'pnl_pct': -0.03, 'holding_days': 5, 'entry_regime': 'CRISIS'},
        ]
        # Mock regime series with enough days
        regimes = pd.Series({pd.Timestamp(f'2024-01-{d:02d}'): 'LOW_VOL' for d in range(2, 30)})
        regimes.loc[pd.Timestamp('2024-02-01')] = 'CRISIS'
        by_regime = ub.aggregate_per_regime(trades, regimes)
        self.assertEqual(by_regime['LOW_VOL']['trade_count'], 2)
        self.assertEqual(by_regime['CRISIS']['trade_count'], 1)

    def test_low_sample_regime_nulls_sharpe(self):
        # 3 trades in CRISIS → trade_count < 5 → sharpe forced to None
        trades = [{'pnl_pct': 0.05, 'holding_days': 5, 'entry_regime': 'CRISIS'}] * 3
        regimes = pd.Series({pd.Timestamp('2024-01-02'): 'CRISIS'})
        out = ub.aggregate_per_regime(trades, regimes)
        self.assertIsNone(out['CRISIS']['sharpe'])


if __name__ == '__main__':
    unittest.main()
