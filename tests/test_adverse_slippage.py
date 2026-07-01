"""tests/test_adverse_slippage.py — always-adverse per-fill slippage."""
from __future__ import annotations
import math, sys, unittest
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from backtest import unified_backtest as ub  # noqa: E402


def _bars(closes, highs=None, lows=None, start='2020-01-02'):
    idx = pd.bdate_range(start, periods=len(closes))
    highs = highs if highs is not None else [c * 1.001 for c in closes]
    lows = lows if lows is not None else [c * 0.999 for c in closes]
    return pd.DataFrame({'high': highs, 'low': lows, 'close': closes}, index=idx)


class TestSlippage(unittest.TestCase):
    def test_zero_slippage_byte_identical(self):
        bars = _bars([102.0, 101.0, 105.0], highs=[102.1, 101.1, 105.1], lows=[101.9, 100.9, 104.9])
        a = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5)
        b = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5, slippage_bps=0.0)
        self.assertEqual(a['pnl_pct'], b['pnl_pct'])
        self.assertEqual(a['exit_price'], b['exit_price'])
        self.assertEqual(a['daily_marks'], b['daily_marks'])
        self.assertEqual(a['holding_days'], b['holding_days'])

    def test_long_slippage_is_adverse(self):
        bars = _bars([102.0, 101.0, 105.0], highs=[102.1, 101.1, 105.1], lows=[101.9, 100.9, 104.9])
        base = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5)
        slip = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5, slippage_bps=10.0)
        self.assertLess(slip['pnl_pct'], base['pnl_pct'])            # win shrinks
        self.assertLess(slip['exit_price'], base['exit_price'])      # exit fill worse (lower)
        self.assertEqual(len(slip['daily_marks']), slip['holding_days'])
        comp = 1.0
        for _, r in slip['daily_marks']:
            comp *= (1.0 + r)
        self.assertAlmostEqual(comp - 1.0, slip['pnl_pct'], places=9)  # marks off fills

    def test_short_slippage_is_adverse(self):
        bars = _bars([98.0, 99.0], highs=[98.5, 99.5], lows=[97.5, 98.5])
        base = ub.simulate_trade(bars, bars.index[0], -1, 100.0, 200.0, 1.0, 5)
        slip = ub.simulate_trade(bars, bars.index[0], -1, 100.0, 200.0, 1.0, 5, slippage_bps=10.0)
        self.assertLess(slip['pnl_pct'], base['pnl_pct'])

    def test_roundtrip_drag_two_s(self):
        # long, exit == entry (zero gross move via max_hold at the entry price) -> pnl ~ -2s
        bars = _bars([100.0, 100.0], highs=[100.05, 100.05], lows=[99.95, 99.95])
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 1.0, 1e9, 5, slippage_bps=10.0)
        self.assertAlmostEqual(out['pnl_pct'], -2 * 10.0 / 1e4, places=4)

    def test_stop_fills_worse_than_level(self):
        # 2 bars: the walk starts AFTER the entry bar, so bar[1] (low 94 <= stop 95) triggers
        bars = _bars([100.0, 98.0], highs=[100.1, 99.0], lows=[99.9, 94.0])
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 95.0, 200.0, 5, slippage_bps=10.0)
        self.assertEqual(out['exit_reason'], 'stop')
        self.assertLess(out['exit_price'], 95.0)  # long stop fills BELOW the level (adverse)

    def test_cost_bps_recalibrated(self):
        self.assertEqual(ub.resolve_cost_model_bps('equity'), 10.0)
        self.assertEqual(ub.resolve_cost_model_bps('etp'), 10.0)
        self.assertEqual(ub.resolve_cost_model_bps('option'), 5.0)
        self.assertEqual(ub.resolve_cost_model_bps('crypto'), 25.0)


if __name__ == '__main__':
    unittest.main()
