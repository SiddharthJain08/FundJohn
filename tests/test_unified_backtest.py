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
        self.assertEqual(m['max_dd_pct'], 0.0)

    def test_zero_holding_days_excluded_from_portfolio_curve(self):
        # Degenerate trades with holding_days=0 are dropped from portfolio
        # marking; hit_rate / total_trades still reflect them.
        trades = [{'pnl_pct': 0.05, 'holding_days': 0,
                   'entry_date': '2024-01-02'}] * 10
        m = ub.aggregate_metrics(trades)
        self.assertEqual(m['total_trades'], 10)
        self.assertEqual(m['max_dd_pct'], 0.0)

    def test_consistent_winner_portfolio(self):
        """All trades win the same pct on overlapping dates → daily-marked
        portfolio compounds steadily, no drawdown."""
        trades = [{'pnl_pct': 0.05, 'holding_days': 5,
                   'entry_date': '2024-01-02'}] * 100
        m = ub.aggregate_metrics(trades)
        self.assertEqual(m['total_trades'], 100)
        self.assertGreater(m['return_pct'], 0)
        self.assertEqual(m['hit_rate'], 1.0)
        # Steady winners → near-zero drawdown
        self.assertLess(m['max_dd_pct'], 0.1)
        # Zero-variance daily returns (all trades contribute same per-day) → None sharpe
        self.assertIsNone(m['sharpe'])

    def test_portfolio_drawdown_realistic(self):
        """When losing trades cluster in time vs winners spread out, the
        portfolio shows a real drawdown — not a 99% cumprod artifact."""
        # 100 winners spread across Jan, 100 losers spread across Feb,
        # 100 winners across March.
        trades = []
        for d in range(2, 30):
            trades.append({'pnl_pct': 0.02, 'holding_days': 3,
                           'entry_date': f'2024-01-{d:02d}'})
        for d in range(2, 28):
            trades.append({'pnl_pct': -0.05, 'holding_days': 3,
                           'entry_date': f'2024-02-{d:02d}'})
        for d in range(2, 30):
            trades.append({'pnl_pct': 0.02, 'holding_days': 3,
                           'entry_date': f'2024-03-{d:02d}'})
        m = ub.aggregate_metrics(trades)
        # Should show a real drawdown bounded below the 99% cumprod artifact.
        # ~28 consecutive days of -1.67%/day compounds to ~38% drawdown,
        # which is the right portfolio answer for this contrived scenario.
        self.assertGreater(m['max_dd_pct'], 0.5)
        self.assertLess(m['max_dd_pct'], 60.0,
                        'portfolio max_dd should be bounded, not 99%+ cumprod artifact')

    def test_no_99pct_drawdown_on_parallel_trades(self):
        """Regression: 2000 trades firing on the same date with mixed wins/losses
        should NOT compound to a 99% drawdown like the old cumprod did."""
        trades = []
        # 1000 mild winners + 1000 mild losers, all on the same entry date
        for _ in range(1000):
            trades.append({'pnl_pct': 0.03, 'holding_days': 5,
                           'entry_date': '2024-01-02'})
        for _ in range(1000):
            trades.append({'pnl_pct': -0.03, 'holding_days': 5,
                           'entry_date': '2024-01-02'})
        m = ub.aggregate_metrics(trades)
        # Equal-weight portfolio of equal +/- trades on same date ≈ flat
        self.assertLess(m['max_dd_pct'], 1.0,
                        'parallel +/-3% trades should net ~flat, not 99% drawdown')


class TestAggregatePerRegime(unittest.TestCase):
    def test_groups_trades_by_entry_regime(self):
        trades = [
            {'pnl_pct': 0.05, 'holding_days': 5, 'entry_regime': 'LOW_VOL',
             'entry_date': '2024-01-02'},
            {'pnl_pct': 0.10, 'holding_days': 5, 'entry_regime': 'LOW_VOL',
             'entry_date': '2024-01-03'},
            {'pnl_pct': -0.03, 'holding_days': 5, 'entry_regime': 'CRISIS',
             'entry_date': '2024-02-01'},
        ]
        regimes = pd.Series({pd.Timestamp(f'2024-01-{d:02d}'): 'LOW_VOL' for d in range(2, 30)})
        regimes.loc[pd.Timestamp('2024-02-01')] = 'CRISIS'
        by_regime = ub.aggregate_per_regime(trades, regimes)
        self.assertEqual(by_regime['LOW_VOL']['trade_count'], 2)
        self.assertEqual(by_regime['CRISIS']['trade_count'], 1)

    def test_low_sample_regime_nulls_sharpe(self):
        # 3 trades in CRISIS → trade_count < 5 → sharpe forced to None
        trades = [{'pnl_pct': 0.05, 'holding_days': 5, 'entry_regime': 'CRISIS',
                   'entry_date': '2024-01-02'}] * 3
        regimes = pd.Series({pd.Timestamp('2024-01-02'): 'CRISIS'})
        out = ub.aggregate_per_regime(trades, regimes)
        self.assertIsNone(out['CRISIS']['sharpe'])


if __name__ == '__main__':
    unittest.main()
