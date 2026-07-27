"""tests/test_adverse_slippage.py — always-adverse per-fill slippage."""
from __future__ import annotations
import contextlib, os, sys, unittest
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from backtest import unified_backtest as ub  # noqa: E402

_FLAG_VARS = ('OPENCLAW_TRUE_MTM_MARKS', 'OPENCLAW_BACKTEST_SLIPPAGE')


@contextlib.contextmanager
def _clean_flags(**overrides):
    """Deterministic env for the two corrected-engine flags: unset both,
    then apply any explicit overrides (values must be str, e.g. '0').
    Restores the ambient env on exit (isolates from a live re-backtest
    process that may have these exported)."""
    saved = {k: os.environ.get(k) for k in _FLAG_VARS}
    try:
        for k in _FLAG_VARS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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

    def test_long_target_triggered_slippage(self):
        # bar[1] high 106 >= target 105 -> long target fires; adverse exit fills BELOW 105
        bars = _bars([100.0, 103.0], highs=[100.1, 106.0], lows=[99.9, 102.0])
        base = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 105.0, 5)
        slip = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 105.0, 5, slippage_bps=10.0)
        self.assertEqual(slip['exit_reason'], 'target')
        self.assertLess(slip['exit_price'], 105.0)          # long target fills below the level
        self.assertLess(slip['pnl_pct'], base['pnl_pct'])

    def test_short_stop_triggered_slippage(self):
        # short (dir=-1): bar[1] high 106 >= stop 105 -> stop fires; adverse exit fills ABOVE 105
        bars = _bars([100.0, 103.0], highs=[100.1, 106.0], lows=[99.9, 102.0])
        base = ub.simulate_trade(bars, bars.index[0], -1, 100.0, 105.0, 1.0, 5)
        slip = ub.simulate_trade(bars, bars.index[0], -1, 100.0, 105.0, 1.0, 5, slippage_bps=10.0)
        self.assertEqual(slip['exit_reason'], 'stop')
        self.assertGreater(slip['exit_price'], 105.0)       # short stop fills above the level (buy to cover)
        self.assertLess(slip['pnl_pct'], base['pnl_pct'])

    def test_cost_bps_recalibrated(self):
        self.assertEqual(ub.resolve_cost_model_bps('equity'), 10.0)
        self.assertEqual(ub.resolve_cost_model_bps('etp'), 10.0)
        self.assertEqual(ub.resolve_cost_model_bps('option'), 5.0)
        self.assertEqual(ub.resolve_cost_model_bps('crypto'), 25.0)


class TestSlippageEnvDefaultOn(unittest.TestCase):
    """2026-07-05 cutover: OPENCLAW_BACKTEST_SLIPPAGE is now default-ON at
    the run_backtest env-read site (unified_backtest.py ~line 851) — the
    instrument's INSTRUMENT_COST_BPS is threaded into simulate_trade's
    slippage_bps whenever the flag resolves truthy, which is now the case
    with NO env var set at all. `=0` is the sole escape hatch back to the
    pre-fix zero-slippage engine. These are ENV-level tests (through
    run_backtest), distinct from the FUNCTION-level tests above which pass
    slippage_bps directly to simulate_trade and are unaffected by this flag."""

    def _run(self, **env_overrides):
        from tests.backtest.test_backtest_fill_model import (
            _make_stub_cls, _run_capture, _trivial_dataset,
        )
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()  # LONG by default
        with _clean_flags(**env_overrides):
            return _run_capture(stub, close_wide, bars, regimes)

    def test_default_applies_slippage_vs_disabled(self):
        trades_on = self._run()                                   # no env -> default ON
        trades_off = self._run(OPENCLAW_BACKTEST_SLIPPAGE='0')     # escape hatch
        self.assertTrue(trades_on and trades_off)
        self.assertEqual(len(trades_on), len(trades_off))
        checked = 0
        for t_on, t_off in zip(trades_on, trades_off):
            self.assertEqual(t_on['exit_date'], t_off['exit_date'])
            self.assertEqual(t_on['exit_reason'], t_off['exit_reason'])
            if t_on['holding_days'] <= 0:
                # zero-holding-day edge case (simulate_trade's empty-window
                # return bypasses slippage entirely) — identical either way.
                continue
            checked += 1
            # adverse fill on a long: slipped exit is strictly worse (lower)
            self.assertLess(t_on['exit_price'], t_off['exit_price'])
            self.assertLess(t_on['pnl_pct'], t_off['pnl_pct'])
        self.assertGreater(checked, 0, 'fixture should produce at least one held trade to compare')

    def test_explicit_zero_matches_legacy_zero_slippage(self):
        """OPENCLAW_BACKTEST_SLIPPAGE=0 must reproduce the pre-fix
        zero-slippage fill exactly (byte-identical escape hatch)."""
        trades_off = self._run(OPENCLAW_BACKTEST_SLIPPAGE='0')
        trades_legacy = self._run(OPENCLAW_TRUE_MTM_MARKS='0', OPENCLAW_BACKTEST_SLIPPAGE='0')
        self.assertTrue(trades_off and trades_legacy)
        self.assertEqual(len(trades_off), len(trades_legacy))
        for a, b in zip(trades_off, trades_legacy):
            self.assertEqual(a['exit_price'], b['exit_price'])
            self.assertEqual(a['pnl_pct'], b['pnl_pct'])


if __name__ == '__main__':
    unittest.main()
