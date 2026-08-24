"""Tests for src/backtest/factor_prescreen.py (Task R2).

Synthetic only — ZZT tickers, monkeypatched price loader (load_price_window
is patched on the module so no real parquet is ever touched). Run only this
file: python3 -m pytest tests/backtest/test_factor_prescreen.py -q
(or) python3 -m unittest tests.backtest.test_factor_prescreen -v
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import factor_prescreen as fp  # noqa: E402
from strategies.base import Signal  # noqa: E402


def _synthetic_prices(n_days: int, tickers=('ZZT1', 'ZZT2', 'ZZT3')):
    """A deterministic date x ticker close panel — no real parquet involved."""
    idx = pd.bdate_range(end='2026-08-21', periods=n_days)
    data = {t: [100.0 + 10 * i + j for j in range(n_days)] for i, t in enumerate(tickers)}
    return pd.DataFrame(data, index=idx), list(tickers)


def _synthetic_loader(universe_source='fallback', tickers=('ZZT1', 'ZZT2', 'ZZT3')):
    """Builds a load_price_window(days, max_tickers, lookback) stand-in
    returning a fixed universe_source — lets tests exercise both branches of
    the zero_signals / universe_source ruling without a real
    declared-universe resolution path existing yet (see
    factor_prescreen.load_price_window). Ignores `lookback` and always
    returns `days + 5` rows total (same shape as before the history/decision
    window fix), so existing exact-count assertions on the `days`-sized
    decision window stay valid — the lookback-specific behaviour is
    exercised separately, via a loader that actually varies with it."""
    def _loader(days, max_tickers, lookback=None):
        prices, universe = _synthetic_prices(n_days=days + 5, tickers=tickers)
        return prices, universe, universe_source
    return _loader


def _synthetic_loader_history_aware(universe_source='fallback', tickers=('ZZT1', 'ZZT2', 'ZZT3')):
    """Unlike _synthetic_loader (which ignores `lookback` and always returns
    a fixed `days + 5` rows, to keep the pre-existing exact-count
    assertions stable), this loader actually HONORS `lookback` — returning
    `days + lookback` total rows, the same history/decision-window contract
    the real load_price_window() now implements. Used by the R2b regression
    test below to prove a long-lookback strategy sees real history."""
    def _loader(days, max_tickers, lookback=None):
        n_days = days + (lookback or 0)
        prices, universe = _synthetic_prices(n_days=n_days, tickers=tickers)
        return prices, universe, universe_source
    return _loader


def _sig(ticker: str, direction: str = 'LONG') -> Signal:
    return Signal(
        ticker=ticker, direction=direction, entry_price=100.0, stop_loss=95.0,
        target_1=105.0, target_2=110.0, target_3=120.0,
        position_size_pct=0.05, confidence='MED',
    )


STRATEGY_MOMENTUM = """
from strategies.base import BaseStrategy, Signal

class ZZTMomentumStrategy(BaseStrategy):
    id = "ZZT_momentum"
    name = "ZZT momentum test strategy"

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return [
            Signal(ticker=t, direction="LONG", entry_price=100.0, stop_loss=95.0,
                   target_1=105.0, target_2=110.0, target_3=120.0,
                   position_size_pct=0.05, confidence="MED")
            for t in universe
        ]
"""

STRATEGY_ZERO = """
from strategies.base import BaseStrategy, Signal

class ZZTZeroStrategy(BaseStrategy):
    id = "ZZT_zero"
    name = "ZZT zero-signal test strategy"

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return []
"""

STRATEGY_CONSTANT = """
from strategies.base import BaseStrategy, Signal

class ZZTConstantStrategy(BaseStrategy):
    id = "ZZT_constant"
    name = "ZZT constant-output test strategy"

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return [Signal(ticker="ZZT1", direction="LONG", entry_price=100.0, stop_loss=95.0,
                        target_1=105.0, target_2=110.0, target_3=120.0,
                        position_size_pct=0.05, confidence="MED")]
"""

STRATEGY_RAISES = """
from strategies.base import BaseStrategy, Signal

class ZZTRaisesStrategy(BaseStrategy):
    id = "ZZT_raises"
    name = "ZZT raising test strategy"

    def generate_signals(self, prices, regime, universe, aux_data=None):
        raise RuntimeError("synthetic strategy blew up")
"""

# R2b regression fixture: mirrors S24_52wk_high_proximity's shape — needs a
# long trailing window (200 bars, standing in for a 52-week/252-bar high
# lookback) before it will emit anything at all. Before the history/
# decision-window fix, load_price_window() only ever loaded `days` trading
# days of history total, so every one of the `days` expanding-window calls
# saw a truncated panel and this always returned [] regardless of the real
# strategy logic — exactly the false zero-signal the fix addresses.
STRATEGY_LONG_LOOKBACK = """
from strategies.base import BaseStrategy, Signal

class ZZTLongLookbackStrategy(BaseStrategy):
    id = "ZZT_long_lookback"
    name = "ZZT long-lookback (52-week-high-style) test strategy"

    def generate_signals(self, prices, regime, universe, aux_data=None):
        if len(prices) < 200:
            return []
        return [
            Signal(ticker=t, direction="LONG", entry_price=100.0, stop_loss=95.0,
                   target_1=105.0, target_2=110.0, target_3=120.0,
                   position_size_pct=0.05, confidence="MED")
            for t in universe
        ]
"""

# R2b-ext regression fixtures (min_lookback-aware floor/cap extension,
# 2026-08-24): these DECLARE the min_lookback class attribute directly —
# same attribute BaseStrategy defaults to 20 and unified_backtest.py reads
# via getattr(instance, 'min_lookback', 20) — unlike STRATEGY_LONG_LOOKBACK
# above, which deliberately leaves it undeclared (inherits the base
# default) to keep the earlier history/decision-window tests decoupled
# from this floor/cap logic. generate_signals is unconditional (unlike
# STRATEGY_LONG_LOOKBACK's len(prices)-gated body) — these two fixtures
# test the FLOOR/CAP ARITHMETIC on stats.lookback_used /
# stats.declared_min_lookback, not a history-gated signal outcome.
STRATEGY_MIN_LOOKBACK_500 = """
from strategies.base import BaseStrategy, Signal

class ZZTMinLookback500Strategy(BaseStrategy):
    id = "ZZT_min_lookback_500"
    name = "ZZT min_lookback=500 test strategy"
    min_lookback = 500

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return [
            Signal(ticker=t, direction="LONG", entry_price=100.0, stop_loss=95.0,
                   target_1=105.0, target_2=110.0, target_3=120.0,
                   position_size_pct=0.05, confidence="MED")
            for t in universe
        ]
"""

STRATEGY_MIN_LOOKBACK_2000 = """
from strategies.base import BaseStrategy, Signal

class ZZTMinLookback2000Strategy(BaseStrategy):
    id = "ZZT_min_lookback_2000"
    name = "ZZT min_lookback=2000 (above-cap) test strategy"
    min_lookback = 2000

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return [
            Signal(ticker=t, direction="LONG", entry_price=100.0, stop_loss=95.0,
                   target_1=105.0, target_2=110.0, target_3=120.0,
                   position_size_pct=0.05, confidence="MED")
            for t in universe
        ]
"""

# Module-level INSTRUMENT_CLASS constant — AST-detected by
# factor_prescreen._resolve_instrument_class the same way
# unified_backtest.py resolves it for a strategy not yet in manifest.json.
# generate_signals always returns [] here on purpose: an options strategy's
# real logic is gated on aux_data['options'], which this screen never
# populates, so [] is what it would ALWAYS see regardless of legitimacy —
# exactly the false positive the aux-dependent bypass exists to avoid.
STRATEGY_OPTION = """
from strategies.base import BaseStrategy, Signal

INSTRUMENT_CLASS = "option"

class ZZTOptionStrategy(BaseStrategy):
    id = "ZZT_option"
    name = "ZZT option test strategy"

    def generate_signals(self, prices, regime, universe, aux_data=None):
        opts = (aux_data or {}).get('options', {})
        return []
"""

# S21-style fixture (controller ruling 2026-08-24, widening Ruling 1): no
# INSTRUMENT_CLASS declared at all — isolates the NEW _module_reads_aux_data
# heuristic from the pre-existing instrument_class=='option' path. The body
# is a byte-for-byte match of the real pattern in
# src/strategies/implementations/S21_iv_hv_spread.py:
#     opts_map = (aux_data or {}).get('options', {})
STRATEGY_AUX_READS = """
from strategies.base import BaseStrategy, Signal

class ZZTAuxReadsStrategy(BaseStrategy):
    id = "ZZT_aux_reads"
    name = "ZZT aux-reads test strategy"

    def generate_signals(self, prices, regime, universe, aux_data=None):
        opts_map = (aux_data or {}).get('options', {})
        return []
"""

# Same subscript idiom, direct (no .get(...)) — exercises the other branch
# of _module_reads_aux_data's detection.
STRATEGY_AUX_SUBSCRIPT = """
from strategies.base import BaseStrategy, Signal

class ZZTAuxSubscriptStrategy(BaseStrategy):
    id = "ZZT_aux_subscript"
    name = "ZZT aux-subscript test strategy"

    def generate_signals(self, prices, regime, universe, aux_data=None):
        opts_map = aux_data['options']
        return []
"""

# Accepts the aux_data parameter but never reads it anywhere in the body —
# must NOT be bypassed (merely accepting the parameter is not aux-dependence).
STRATEGY_AUX_PARAM_UNUSED = """
from strategies.base import BaseStrategy, Signal

class ZZTAuxUnusedStrategy(BaseStrategy):
    id = "ZZT_aux_unused"
    name = "ZZT aux-param-unused test strategy"

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return [
            Signal(ticker=t, direction="LONG", entry_price=100.0, stop_loss=95.0,
                   target_1=105.0, target_2=110.0, target_3=120.0,
                   position_size_pct=0.05, confidence="MED")
            for t in universe
        ]
"""


def _write_strategy(tmpdir: str, name: str, source: str) -> str:
    path = Path(tmpdir) / f'{name}.py'
    path.write_text(textwrap.dedent(source))
    return str(path)


class FactorPrescreenCLITests(unittest.TestCase):
    """Drives factor_prescreen.main() in-process with a monkeypatched
    load_price_window — exercises the exact CLI/exit-code contract without
    spawning a subprocess or touching data/master/prices.parquet."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def _run(self, strategy_path: str, days: int = 5, max_tickers: int = 10,
              loader=None, lookback=None):
        loader = loader or _synthetic_loader()
        argv = ['--strategy-file', strategy_path,
                '--days', str(days), '--max-tickers', str(max_tickers)]
        if lookback is not None:
            argv += ['--lookback', str(lookback)]
        stdout = io.StringIO()
        with patch.object(fp, 'load_price_window', side_effect=loader):
            with contextlib.redirect_stdout(stdout):
                code = fp.main(argv)
        return code, stdout.getvalue()

    def test_momentum_strategy_passes_with_exact_signal_count(self):
        path = _write_strategy(self._tmpdir.name, 'zzt_momentum', STRATEGY_MOMENTUM)
        code, out = self._run(path, days=5)
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result['pass'])
        self.assertIsNone(result['reason'])
        # 3 universe tickers/day * 5 driven days = 15, deterministic fixture.
        self.assertEqual(result['stats']['signals_total'], 15)
        self.assertEqual(result['stats']['active_days'], 5)
        self.assertEqual(result['stats']['unique_tickers'], 3)
        self.assertEqual(result['stats']['direction_balance'], 1.0)
        self.assertEqual(result['stats']['universe_source'], 'fallback')
        # Wire contract the orchestrator's emitGateDecision metadata depends on.
        self.assertEqual(
            set(result['stats'].keys()),
            {'signals_total', 'active_days', 'direction_balance', 'unique_tickers',
             'turnover_proxy', 'universe_source', 'history_bars',
             'lookback_used', 'declared_min_lookback'})
        # 10 total rows loaded by the synthetic loader (days=5 + 5 pad);
        # history_bars records the FULL loaded panel, not just the decision
        # window driven above.
        self.assertEqual(result['stats']['history_bars'], 10)
        # STRATEGY_MOMENTUM doesn't declare its own min_lookback, so it
        # inherits BaseStrategy's default (20) — never None/absent in
        # practice for a real BaseStrategy subclass. declared_min_lookback
        # + 10 (30) is below the default --lookback (300, no --lookback
        # passed here), so lookback_used stays the unfloored default.
        # (_synthetic_loader itself ignores whatever lookback_used it's
        # called with — see its docstring — so history_bars above is
        # unaffected by this floor.)
        self.assertEqual(result['stats']['declared_min_lookback'], 20)
        self.assertEqual(result['stats']['lookback_used'], 300)

    def test_zero_signal_on_fallback_universe_soft_passes(self):
        # Controller ruling 2026-08-24 (concern-2 fix): zero_signals on the
        # most-liquid FALLBACK universe must NOT hard-block — it's annotated
        # as a soft pass instead, since the fallback universe may simply not
        # be where this strategy's real edge lives.
        path = _write_strategy(self._tmpdir.name, 'zzt_zero', STRATEGY_ZERO)
        code, out = self._run(path, days=5, loader=_synthetic_loader('fallback'))
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result['pass'])
        self.assertEqual(result['reason'], 'zero_signals_on_fallback_universe')
        self.assertEqual(result['stats']['signals_total'], 0)
        self.assertEqual(result['stats']['active_days'], 0)
        self.assertIsNone(result['stats']['direction_balance'])
        self.assertIsNone(result['stats']['turnover_proxy'])
        self.assertEqual(result['stats']['universe_source'], 'fallback')

    def test_zero_signal_on_declared_universe_hard_fails(self):
        # Controller ruling 2026-08-24: hard-block on zero_signals is
        # preserved when the universe came from the strategy's OWN resolved
        # universe (universe_source='declared') rather than the fallback —
        # there is no false-positive risk in that case.
        path = _write_strategy(self._tmpdir.name, 'zzt_zero', STRATEGY_ZERO)
        code, out = self._run(path, days=5, loader=_synthetic_loader('declared'))
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason'], 'zero_signals')
        self.assertEqual(result['stats']['signals_total'], 0)
        self.assertEqual(result['stats']['universe_source'], 'declared')

    def test_constant_output_hard_fails_on_fallback_universe(self):
        # constant_output stays a hard block regardless of universe_source.
        path = _write_strategy(self._tmpdir.name, 'zzt_constant', STRATEGY_CONSTANT)
        code, out = self._run(path, days=5, loader=_synthetic_loader('fallback'))
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason'], 'constant_output')
        self.assertEqual(result['stats']['unique_tickers'], 1)
        self.assertEqual(result['stats']['active_days'], 5)
        self.assertEqual(result['stats']['universe_source'], 'fallback')

    def test_constant_output_hard_fails_on_declared_universe(self):
        # ... and also when the universe is the strategy's own declared one.
        path = _write_strategy(self._tmpdir.name, 'zzt_constant', STRATEGY_CONSTANT)
        code, out = self._run(path, days=5, loader=_synthetic_loader('declared'))
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(result['pass'])
        self.assertEqual(result['reason'], 'constant_output')
        self.assertEqual(result['stats']['universe_source'], 'declared')

    def test_option_instrument_class_bypasses_prescreen(self):
        # Controller ruling 2026-08-24 (concern-1 fix): an instrument_class=
        # 'option' strategy skips the screen entirely rather than being
        # hard-blocked as zero_signals (which would be a screen artifact,
        # not a real finding — this prescreen never populates real
        # aux_data['options']).
        path = _write_strategy(self._tmpdir.name, 'zzt_option', STRATEGY_OPTION)
        code, out = self._run(path, days=5)
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result['pass'])
        self.assertEqual(result['reason'], 'prescreen_skipped_aux_dependent')
        self.assertIsNone(result['stats'])

    def test_aux_data_read_via_get_idiom_bypasses_prescreen(self):
        # Controller ruling 2026-08-24, widening Ruling 1: an S21-style
        # strategy (no INSTRUMENT_CLASS declared — manifest-classified
        # 'equity' in real life) that reads `(aux_data or {}).get('options',
        # {})` is bypassed by the NEW AST heuristic alone, same outcome as
        # the instrument_class=='option' path.
        path = _write_strategy(self._tmpdir.name, 'zzt_aux_reads', STRATEGY_AUX_READS)
        code, out = self._run(path, days=5)
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result['pass'])
        self.assertEqual(result['reason'], 'prescreen_skipped_aux_dependent')
        self.assertIsNone(result['stats'])

    def test_aux_data_param_unused_is_still_screened(self):
        # Controller ruling 2026-08-24: merely ACCEPTING the aux_data
        # parameter (never reading it) must NOT trigger the bypass — this
        # strategy goes through full normal screening and should pass
        # cleanly with real stats (same deterministic shape as the momentum
        # fixture), proving the bypass did NOT fire.
        path = _write_strategy(self._tmpdir.name, 'zzt_aux_unused', STRATEGY_AUX_PARAM_UNUSED)
        code, out = self._run(path, days=5)
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result['pass'])
        self.assertIsNone(result['reason'])
        self.assertIsNotNone(result['stats'])
        self.assertEqual(result['stats']['signals_total'], 15)
        self.assertEqual(result['stats']['universe_source'], 'fallback')

    def test_exception_inside_generate_signals_is_infra_failure(self):
        path = _write_strategy(self._tmpdir.name, 'zzt_raises', STRATEGY_RAISES)
        code, out = self._run(path, days=5)
        self.assertEqual(code, 1)
        self.assertEqual(out, '')  # no JSON on infra failure

    def test_missing_strategy_file_is_infra_failure(self):
        code, out = self._run('/nonexistent/path/to/S_ghost.py', days=5)
        self.assertEqual(code, 1)
        self.assertEqual(out, '')

    def test_long_lookback_strategy_produces_signals_with_sufficient_lookback(self):
        # R2b regression test: a strategy needing >=200 bars of history
        # (standing in for S24_52wk_high_proximity's real 52-week-high
        # lookback) returned [] on EVERY driven day before the history/
        # decision-window split existed, because load_price_window() only
        # ever loaded `days` trading days of history total. With
        # lookback=300 (>= the 200-bar requirement) and days=60, every one
        # of the 60 driven decision bars now carries >=200 bars of leading
        # history and the strategy produces real signals.
        path = _write_strategy(self._tmpdir.name, 'zzt_long_lookback', STRATEGY_LONG_LOOKBACK)
        loader = _synthetic_loader_history_aware()
        code, out = self._run(path, days=60, max_tickers=10, loader=loader, lookback=300)
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result['pass'])
        self.assertIsNone(result['reason'])
        # 60 driven days * 3 universe tickers, deterministic fixture.
        self.assertEqual(result['stats']['signals_total'], 180)
        self.assertEqual(result['stats']['active_days'], 60)
        self.assertEqual(result['stats']['history_bars'], 360)  # days(60) + lookback_used(300)
        # STRATEGY_LONG_LOOKBACK doesn't declare its own min_lookback
        # (inherits BaseStrategy's default 20); 20+10=30 is below the
        # explicit --lookback=300 passed here, so lookback_used is
        # unfloored.
        self.assertEqual(result['stats']['declared_min_lookback'], 20)
        self.assertEqual(result['stats']['lookback_used'], 300)

    def test_long_lookback_strategy_zero_signals_without_lookback(self):
        # Same strategy and days, but lookback=0 reproduces the PRE-FIX
        # shape (history == the days-sized decision window, nothing extra):
        # every driven day sees < 200 bars of history and the strategy
        # returns [] on all of them — proving the bug this fix addresses
        # actually existed, and that it's the lookback split (not some
        # other change) that resolves it. history_bars is 90, not 60: the
        # NEW min_lookback-aware floor (extension, 2026-08-24) still bumps
        # the effective lookback up to BaseStrategy's inherited default
        # (declared_min_lookback=20) + the 10-bar pad = 30, even though
        # this fixture never declares min_lookback itself and the caller
        # asked for lookback=0 — that floor is universal (see
        # run_prescreen's docstring), not opt-in. 30 is still nowhere near
        # this fixture's internal >=200-bar requirement, so the zero-signal
        # outcome this test exists to pin is unaffected.
        path = _write_strategy(self._tmpdir.name, 'zzt_long_lookback', STRATEGY_LONG_LOOKBACK)
        loader = _synthetic_loader_history_aware()
        code, out = self._run(path, days=60, max_tickers=10, loader=loader, lookback=0)
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result['pass'])
        self.assertEqual(result['reason'], 'zero_signals_on_fallback_universe')
        self.assertEqual(result['stats']['signals_total'], 0)
        self.assertEqual(result['stats']['declared_min_lookback'], 20)
        self.assertEqual(result['stats']['lookback_used'], 30)  # floored: max(0, 20+10)
        self.assertEqual(result['stats']['history_bars'], 90)   # days(60) + lookback_used(30)

    def test_declared_min_lookback_floors_lookback_used(self):
        # R2b-ext regression: a strategy DECLARING min_lookback=500 (above
        # the 300 default --lookback) must be floored up to
        # declared_min_lookback + 10 = 510, not truncated to the flat
        # default — otherwise the same false-zero-signal bug this task
        # fixed for S24 (min_lookback=260) would resurface for any
        # strategy needing more than the default 300-bar lookback (a real,
        # non-hypothetical case — a repo-wide grep found 30 such
        # strategies, several at 504/756/1260).
        path = _write_strategy(self._tmpdir.name, 'zzt_min_lookback_500', STRATEGY_MIN_LOOKBACK_500)
        loader = _synthetic_loader_history_aware()
        # No --lookback passed -> CLI default (300, absent an env override).
        code, out = self._run(path, days=5, max_tickers=10, loader=loader)
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result['pass'])
        self.assertEqual(result['stats']['declared_min_lookback'], 500)
        self.assertEqual(result['stats']['lookback_used'], 510)  # 500 + 10 pad
        # "gets >=510 history bars with the default --lookback 300" (brief wording).
        self.assertGreaterEqual(result['stats']['history_bars'], 510)
        self.assertEqual(result['stats']['history_bars'], 5 + 510)  # exact, via the history-aware loader

    def test_declared_min_lookback_caps_at_max_lookback_bars(self):
        # R2b-ext regression: a pathological/very-long declaration (2000,
        # above MAX_LOOKBACK_BARS=1300) is CAPPED, not honored verbatim —
        # keeps a bad/typo'd min_lookback from blowing up the load.
        path = _write_strategy(self._tmpdir.name, 'zzt_min_lookback_2000', STRATEGY_MIN_LOOKBACK_2000)
        loader = _synthetic_loader_history_aware()
        code, out = self._run(path, days=5, max_tickers=10, loader=loader)
        self.assertEqual(code, 0)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result['pass'])
        self.assertEqual(result['stats']['declared_min_lookback'], 2000)
        self.assertEqual(result['stats']['lookback_used'], 1300)  # capped, NOT 2010
        self.assertEqual(result['stats']['history_bars'], 5 + 1300)


class ComputeStatsTests(unittest.TestCase):
    """Unit-level tests on the pure aggregation function — no CLI, no I/O."""

    def test_turnover_proxy_hand_computed_two_active_days(self):
        daily_signals = [
            [_sig('A'), _sig('B')],
            [_sig('B'), _sig('C')],
        ]
        passed, reason, stats = fp.compute_stats(daily_signals)
        self.assertTrue(passed)
        self.assertIsNone(reason)
        # {A,B} then {B,C}: intersection {B}=1, union {A,B,C}=3 -> 1 - 1/3.
        self.assertAlmostEqual(stats['turnover_proxy'], 1 - 1 / 3, places=6)

    def test_turnover_proxy_none_below_two_active_days(self):
        passed, reason, stats = fp.compute_stats([[_sig('A')], [], []])
        self.assertIsNone(stats['turnover_proxy'])

    def test_direction_balance_none_when_no_directional_signals(self):
        # Only non-LONG/SHORT directions -> long+short count is 0.
        daily_signals = [[_sig('A', direction='FLAT')]]
        _, _, stats = fp.compute_stats(daily_signals)
        self.assertIsNone(stats['direction_balance'])

    def test_all_short_direction_balance_zero(self):
        daily_signals = [[_sig('A', direction='SHORT')], [_sig('A', direction='SHORT')]]
        _, _, stats = fp.compute_stats(daily_signals)
        self.assertEqual(stats['direction_balance'], 0.0)

    def test_zero_signals_defaults_to_fallback_soft_pass(self):
        # compute_stats()'s own default (universe_source='fallback') must
        # match load_price_window()'s real default, so a caller who forgets
        # to pass universe_source explicitly never accidentally hard-blocks.
        passed, reason, stats = fp.compute_stats([[], []])
        self.assertTrue(passed)
        self.assertEqual(reason, 'zero_signals_on_fallback_universe')
        self.assertEqual(stats['universe_source'], 'fallback')

    def test_zero_signals_hard_fails_only_when_declared(self):
        passed, reason, stats = fp.compute_stats([[], []], universe_source='declared')
        self.assertFalse(passed)
        self.assertEqual(reason, 'zero_signals')
        self.assertEqual(stats['universe_source'], 'declared')


class ResolveInstrumentClassTests(unittest.TestCase):
    """Unit-level tests on the pure AST-detection fallback path (precedence
    step 2/3) — the manifest-lookup path (step 1) needs a real
    manifest.json entry keyed by strategy_id, which is out of scope for a
    synthetic-only test file per the brief; AST-detection is what fires for
    every strategy file in these tests anyway (none are manifest-registered)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def test_detects_module_level_instrument_class(self):
        path = _write_strategy(self._tmpdir.name, 'zzt_option', STRATEGY_OPTION)
        self.assertEqual(fp._resolve_instrument_class('ZZT_option_not_in_manifest', path), 'option')

    def test_defaults_to_equity_when_undeclared(self):
        path = _write_strategy(self._tmpdir.name, 'zzt_momentum', STRATEGY_MOMENTUM)
        self.assertEqual(fp._resolve_instrument_class('ZZT_momentum_not_in_manifest', path), 'equity')


class ModuleReadsAuxDataTests(unittest.TestCase):
    """Unit-level tests on the new AST heuristic (controller ruling
    2026-08-24, widening Ruling 1) — no CLI, no I/O beyond writing the
    fixture file itself."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def test_detects_dot_get_idiom(self):
        # (aux_data or {}).get('options', {}) — the real S21_iv_hv_spread.py
        # pattern.
        path = _write_strategy(self._tmpdir.name, 'zzt_aux_reads', STRATEGY_AUX_READS)
        self.assertTrue(fp._module_reads_aux_data(path))

    def test_detects_bare_subscript(self):
        path = _write_strategy(self._tmpdir.name, 'zzt_aux_subscript', STRATEGY_AUX_SUBSCRIPT)
        self.assertTrue(fp._module_reads_aux_data(path))

    def test_does_not_detect_unused_parameter(self):
        # Accepting aux_data=None as a parameter, never reading it, must NOT match.
        path = _write_strategy(self._tmpdir.name, 'zzt_aux_unused', STRATEGY_AUX_PARAM_UNUSED)
        self.assertFalse(fp._module_reads_aux_data(path))

    def test_does_not_detect_when_absent_entirely(self):
        path = _write_strategy(self._tmpdir.name, 'zzt_momentum', STRATEGY_MOMENTUM)
        self.assertFalse(fp._module_reads_aux_data(path))

    def test_false_on_missing_file(self):
        self.assertFalse(fp._module_reads_aux_data('/nonexistent/path/S_ghost.py'))


if __name__ == '__main__':
    unittest.main()
