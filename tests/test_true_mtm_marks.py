"""tests/test_true_mtm_marks.py — true daily mark-to-market (Phase 1a).
simulate_trade emits a real per-day return path; _portfolio_daily_returns
aggregates real marks (restoring volatility) with a smear fallback."""
from __future__ import annotations
import contextlib, math, os, sys, unittest
from pathlib import Path
import pandas as pd, numpy as np

ROOT = Path(__file__).resolve().parents[1]
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

    def test_nonfinite_marks_dropped_not_poisoning(self):
        # a NaN interior mark must NOT reach the aggregate (would cumprod-poison
        # every trade sharing that date). It is dropped; finite marks survive.
        d = pd.bdate_range('2020-01-02', periods=3)
        t = {'pnl_pct': 0.03, 'holding_days': 3, 'entry_date': pd.Timestamp('2020-01-01'),
             'entry_regime': 'LOW_VOL',
             'daily_marks': [(d[0], 0.02), (d[1], float('nan')), (d[2], 0.01)]}
        dr, dates = ub._portfolio_daily_returns([t])
        self.assertTrue(all(np.isfinite(dr)))          # no NaN reaches the series
        self.assertEqual(len(dr), 2)                    # the NaN day dropped, 2 finite remain

    def test_multi_trade_equal_weight_on_shared_date(self):
        # two trades open on the same date -> that date is the equal-weight avg
        d = pd.bdate_range('2020-01-02', periods=2)
        t1 = {'pnl_pct': 0.0, 'holding_days': 2, 'entry_date': pd.Timestamp('2020-01-01'),
              'entry_regime': 'LOW_VOL', 'daily_marks': [(d[0], 0.10), (d[1], -0.02)]}
        t2 = {'pnl_pct': 0.0, 'holding_days': 2, 'entry_date': pd.Timestamp('2020-01-01'),
              'entry_regime': 'LOW_VOL', 'daily_marks': [(d[0], 0.00), (d[1], 0.04)]}
        dr, dates = ub._portfolio_daily_returns([t1, t2])
        self.assertEqual(len(dr), 2)
        self.assertAlmostEqual(float(dr[0]), (0.10 + 0.00) / 2, places=9)
        self.assertAlmostEqual(float(dr[1]), (-0.02 + 0.04) / 2, places=9)


class TestTrueMtmEnvDefaultOn(unittest.TestCase):
    """2026-07-05 cutover: OPENCLAW_TRUE_MTM_MARKS is now default-ON at the
    run_backtest/_per_bar_simulate env-read site (unified_backtest.py
    ~line 589) — a trade's `daily_marks` list is attached whenever the flag
    resolves truthy, which is now the case with NO env var set at all.
    `=0` is the sole escape hatch back to the pre-fix smear-only shape
    (`daily_marks` forced to `[]`). These are ENV-level tests (through
    run_backtest), distinct from the FUNCTION-level tests above which call
    simulate_trade directly and are unaffected by this flag."""

    def _run(self, **env_overrides):
        from tests.test_backtest_fill_model import (
            _make_stub_cls, _run_capture, _trivial_dataset,
        )
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()
        with _clean_flags(**env_overrides):
            return _run_capture(stub, close_wide, bars, regimes)

    def test_no_env_produces_daily_marks_by_default(self):
        trades = self._run()
        self.assertTrue(trades, 'fixture should produce at least one trade')
        # A trade with holding_days==0 (fired on the last fillable bar, no
        # room left to walk) legitimately has daily_marks==[] regardless of
        # the flag (simulate_trade's empty-window case) — that's excluded
        # from _portfolio_daily_returns anyway (hold<=0 skip), so only
        # trades that actually walk at least one bar are checked here.
        held = [t for t in trades if t['holding_days'] > 0]
        self.assertTrue(held, 'fixture should produce at least one held trade')
        for t in held:
            self.assertTrue(t['daily_marks'],
                            'daily_marks must be populated with no env vars set (default-ON)')

    def test_explicit_zero_disables_marks(self):
        trades = self._run(OPENCLAW_TRUE_MTM_MARKS='0')
        held = [t for t in trades if t['holding_days'] > 0]
        self.assertTrue(held, 'fixture should produce at least one held trade')
        for t in held:
            self.assertEqual(t['daily_marks'], [],
                             'daily_marks must be [] when OPENCLAW_TRUE_MTM_MARKS=0 (escape hatch)')


class TestCorrectedEngineDefaultByDefault(unittest.TestCase):
    """Pin: with NO env vars set at all, run_backtest runs the FULLY
    corrected engine — true daily_marks AND adverse slippage both active —
    vs. both flags explicitly ='0' reproducing the pre-fix engine."""

    def test_no_env_vars_corrected_engine_active(self):
        from tests.test_backtest_fill_model import (
            _make_stub_cls, _run_capture, _trivial_dataset,
        )
        close_wide, bars, regimes, dates, closes, opens = _trivial_dataset()
        stub = _make_stub_cls()
        with _clean_flags():
            trades_default = _run_capture(stub, close_wide, bars, regimes)
        with _clean_flags(OPENCLAW_TRUE_MTM_MARKS='0', OPENCLAW_BACKTEST_SLIPPAGE='0'):
            trades_legacy = _run_capture(stub, close_wide, bars, regimes)
        self.assertTrue(trades_default and trades_legacy)
        self.assertEqual(len(trades_default), len(trades_legacy))
        checked = 0
        for d, l in zip(trades_default, trades_legacy):
            self.assertEqual(d['exit_date'], l['exit_date'])
            if d['holding_days'] <= 0:
                # zero-holding-day edge case (no bars left to fill/slip) —
                # identical in both runs by construction; not informative.
                continue
            checked += 1
            self.assertTrue(d['daily_marks'],
                            'no-env run must carry true daily_marks (corrected engine default-ON)')
            self.assertEqual(l['daily_marks'], [],
                             'both-flags=0 run must be the legacy smear shape (escape hatch)')
            self.assertLess(d['exit_price'], l['exit_price'],
                            'no-env run must apply adverse slippage vs the legacy (unslipped) fill')
            self.assertLess(d['pnl_pct'], l['pnl_pct'],
                            'adverse slippage must make the no-env pnl_pct worse than legacy')
        self.assertGreater(checked, 0, 'fixture should produce at least one held trade to compare')


if __name__ == '__main__':
    unittest.main()
