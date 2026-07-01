"""tests/test_true_mtm_marks.py — true daily mark-to-market (Phase 1a).
simulate_trade emits a real per-day return path; _portfolio_daily_returns
aggregates real marks (restoring volatility) with a smear fallback."""
from __future__ import annotations
import math, sys, unittest
from pathlib import Path
import pandas as pd, numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from backtest import unified_backtest as ub  # noqa: E402


def _bars(closes, highs=None, lows=None, start='2020-01-02'):
    idx = pd.bdate_range(start, periods=len(closes))
    highs = highs if highs is not None else [c * 1.001 for c in closes]
    lows = lows if lows is not None else [c * 0.999 for c in closes]
    return pd.DataFrame({'high': highs, 'low': lows, 'close': closes}, index=idx)


class TestSimulateTradeMarks(unittest.TestCase):
    def test_len_equals_holding_days_maxhold_long(self):
        # entry 100; closes rise; target/stop never hit; max_hold=3 -> exit last close
        bars = _bars([102.0, 101.0, 105.0], highs=[102.1, 101.1, 105.1], lows=[101.9, 100.9, 104.9])
        entry = bars.index[0]  # entry_date; walk starts strictly after
        out = ub.simulate_trade(bars, entry, +1, 100.0, 90.0, 200.0, 3)
        # walk is bars strictly after entry -> 2 bars available here
        self.assertEqual(len(out['daily_marks']), out['holding_days'])
        self.assertTrue(all(isinstance(d, pd.Timestamp) for d, _ in out['daily_marks']))

    def test_target_exit_len_and_exit_price_mark(self):
        # bar after entry has high>=target -> target exit at 105
        bars = _bars([100.0, 103.0], highs=[100.5, 106.0], lows=[99.5, 102.0])
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 105.0, 5)
        self.assertEqual(out['exit_reason'], 'target')
        self.assertEqual(out['holding_days'], len(out['daily_marks']))
        # last mark reflects exit at target 105 (from prior mark 100 base): +0.05
        self.assertAlmostEqual(out['daily_marks'][-1][1], 105.0 / 100.0 - 1.0, places=9)

    def test_long_compound_equals_pnl(self):
        bars = _bars([102.0, 101.0, 105.0], highs=[102.1, 101.1, 105.1], lows=[101.9, 100.9, 104.9])
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5)
        comp = 1.0
        for _, r in out['daily_marks']:
            comp *= (1.0 + r)
        self.assertAlmostEqual(comp - 1.0, out['pnl_pct'], places=9)

    def test_short_len_ok_compound_not_asserted(self):
        bars = _bars([98.0, 99.0], highs=[98.5, 99.5], lows=[97.5, 98.5])
        out = ub.simulate_trade(bars, bars.index[0], -1, 100.0, 200.0, 1.0, 5)
        self.assertEqual(out['holding_days'], len(out['daily_marks']))  # path-dependent; only len checked

    def test_empty_window_zero_marks(self):
        bars = _bars([100.0])  # only the entry bar; nothing strictly after
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 110.0, 5)
        self.assertEqual(out['holding_days'], 0)
        self.assertEqual(out['daily_marks'], [])


class TestPortfolioDailyReturns(unittest.TestCase):
    def _trade(self, entry_str, marks, hold, pnl):
        return {'pnl_pct': pnl, 'holding_days': hold, 'entry_date': pd.Timestamp(entry_str),
                'entry_regime': 'LOW_VOL', 'daily_marks': marks}

    def test_marks_restore_volatility_vs_smear(self):
        # same total pnl, but a volatile daily path -> real std >> smear std
        d = pd.bdate_range('2020-01-02', periods=4)
        volatile = [(d[0], 0.10), (d[1], -0.08), (d[2], 0.06), (d[3], -0.02)]  # nets ~+0.05
        t_marks = self._trade('2020-01-01', volatile, 4, 0.05)
        t_smear = {'pnl_pct': 0.05, 'holding_days': 4, 'entry_date': pd.Timestamp('2020-01-01'),
                   'entry_regime': 'LOW_VOL'}  # no daily_marks -> smear
        dr_marks, _ = ub._portfolio_daily_returns([t_marks])
        dr_smear, _ = ub._portfolio_daily_returns([t_smear])
        self.assertGreater(float(dr_marks.std(ddof=1)), 5 * float(dr_smear.std(ddof=1)))

    def test_smear_fallback_when_no_marks(self):
        t = {'pnl_pct': 0.04, 'holding_days': 4, 'entry_date': pd.Timestamp('2020-01-01'),
             'entry_regime': 'LOW_VOL'}  # no daily_marks key
        dr, dates = ub._portfolio_daily_returns([t])
        self.assertEqual(len(dr), 4)
        for r in dr:
            self.assertAlmostEqual(float(r), 0.04 / 4, places=9)  # flat smear (byte-identical)

    def test_empty_marks_uses_smear(self):
        t = {'pnl_pct': 0.04, 'holding_days': 4, 'entry_date': pd.Timestamp('2020-01-01'),
             'entry_regime': 'LOW_VOL', 'daily_marks': []}  # flag-OFF shape
        dr, _ = ub._portfolio_daily_returns([t])
        self.assertEqual(len(dr), 4)
        self.assertAlmostEqual(float(dr[0]), 0.01, places=9)


if __name__ == '__main__':
    unittest.main()
