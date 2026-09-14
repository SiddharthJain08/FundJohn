"""A2 (spec 2026-09-12 §1): a bar that OPENS beyond a bracket level fills at
that open, not at the untouched level. Behind OPENCLAW_BT_GAP_FILL=open;
unset/'level' is byte-identical to the pre-2026-09-12 engine.

tests/backtest/conftest.py is autouse and pins OPENCLAW_BT_FILL_MODEL=close
(legacy t+1); these tests call simulate_trade directly so that pin is inert,
but the frozen-value expectations below are written in t+1 geometry anyway.
"""
from __future__ import annotations

import contextlib
import os

import pandas as pd
import pytest

import backtest.unified_backtest as ub
from backtest.open_book import OpenTrade, advance_open_book

_FLAG_VARS = ('OPENCLAW_BT_GAP_FILL', 'OPENCLAW_BT_DOUBLE_TOUCH',
              'OPENCLAW_BACKTEST_SLIPPAGE', 'OPENCLAW_TRUE_MTM_MARKS')


@contextlib.contextmanager
def _clean_flags(**overrides):
    """Unset every flag this file's behaviour depends on, then apply overrides.
    Mirrors tests/backtest/test_adverse_slippage.py::_clean_flags, whose
    _FLAG_VARS predates OPENCLAW_BT_GAP_FILL."""
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


def _ohlc(rows, start='2024-01-02'):
    """rows: (open, high, low, close) per bar."""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame({'open': [r[0] for r in rows], 'high': [r[1] for r in rows],
                         'low': [r[2] for r in rows], 'close': [r[3] for r in rows]}, index=idx)


def _hl(rows, start='2024-01-02'):
    """Same bars WITHOUT an 'open' column — the shape several existing test
    helpers build (tests/backtest/test_adverse_slippage.py::_bars)."""
    return _ohlc(rows, start=start).drop(columns=['open'])


class TestBarExitGapRule:
    def test_long_stop_gap_fills_at_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(1, high=96.0, low=94.0, stop_loss=98.0, target_1=108.0,
                                dt_priority='stop', open_=95.0) == (95.0, 'stop')

    def test_short_stop_gap_fills_at_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(-1, high=108.0, low=104.0, stop_loss=102.0, target_1=92.0,
                                dt_priority='stop', open_=106.0) == (106.0, 'stop')

    def test_long_target_gap_fills_at_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(1, high=112.0, low=110.0, stop_loss=95.0, target_1=108.0,
                                dt_priority='stop', open_=111.0) == (111.0, 'target')

    def test_short_target_gap_fills_at_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(-1, high=90.0, low=88.0, stop_loss=105.0, target_1=92.0,
                                dt_priority='stop', open_=89.0) == (89.0, 'target')

    def test_no_gap_is_unchanged_under_the_flag(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(1, high=101.0, low=94.0, stop_loss=95.0, target_1=108.0,
                                dt_priority='stop', open_=100.0) == (95.0, 'stop')
            assert ub._bar_exit(1, high=101.0, low=99.0, stop_loss=95.0, target_1=108.0,
                                dt_priority='stop', open_=100.0) == (None, None)

    def test_double_touch_priority_unchanged_when_the_open_is_inside(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            both = dict(high=110.0, low=90.0, stop_loss=95.0, target_1=108.0, open_=100.0)
            assert ub._bar_exit(1, dt_priority='stop', **both) == (95.0, 'stop')
            assert ub._bar_exit(1, dt_priority='target', **both) == (108.0, 'target')

    def test_flag_unset_ignores_the_open(self):
        with _clean_flags():
            assert ub._bar_exit(1, high=96.0, low=94.0, stop_loss=98.0, target_1=108.0,
                                dt_priority='stop', open_=95.0) == (98.0, 'stop')

    def test_flag_level_ignores_the_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='level'):
            assert ub._bar_exit(1, high=96.0, low=94.0, stop_loss=98.0, target_1=108.0,
                                dt_priority='stop', open_=95.0) == (98.0, 'stop')

    def test_open_none_ignores_the_flag(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            assert ub._bar_exit(1, high=96.0, low=94.0, stop_loss=98.0, target_1=108.0,
                                dt_priority='stop', open_=None) == (98.0, 'stop')


class TestSimulateTrade:
    BARS = _ohlc([(100.0, 100.5, 99.5, 100.2),      # entry bar (walk starts after it)
                  (95.0, 96.0, 94.0, 95.5)])         # gaps down through stop=98

    def test_gap_flag_fills_the_stop_at_the_open(self):
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open', OPENCLAW_BACKTEST_SLIPPAGE='0'):
            out = ub.simulate_trade(self.BARS, self.BARS.index[0], +1, 100.0, 98.0, 108.0, 5)
        assert out['exit_reason'] == 'stop'
        assert out['exit_price'] == pytest.approx(95.0)
        assert out['pnl_pct'] == pytest.approx(-0.05)

    def test_flag_unset_reproduces_todays_frozen_values(self):
        """FROZEN: these are the numbers the engine produces today. If this
        test moves, the 'unset == byte-identical' contract is broken."""
        with _clean_flags():
            out = ub.simulate_trade(self.BARS, self.BARS.index[0], +1, 100.0, 98.0, 108.0, 5)
        assert out['exit_reason'] == 'stop'
        assert out['exit_price'] == pytest.approx(98.0)
        assert out['pnl_pct'] == pytest.approx(-0.02)
        assert out['holding_days'] == 1

    def test_missing_open_column_falls_back_to_level(self):
        bars = _hl([(100.0, 100.5, 99.5, 100.2), (95.0, 96.0, 94.0, 95.5)])
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 98.0, 108.0, 5)
        assert out['exit_reason'] == 'stop'
        assert out['exit_price'] == pytest.approx(98.0)


class TestOpenBookStepper:
    def test_stepper_passes_the_bar_open(self):
        bars = _ohlc([(100.0, 100.5, 99.5, 100.2), (95.0, 96.0, 94.0, 95.5)])
        by_ticker = {'AAA': bars}

        class _NoHook:
            exit_hook = False

        trade = OpenTrade(ticker='AAA', direction=1, entry_date=bars.index[0],
                          entry_price=100.0, entry_fill=100.0, stop_loss=98.0,
                          target_1=108.0, hold_cap=21, entry_regime='LOW_VOL',
                          signal_params={}, slippage=0.0, prev_mark=100.0)
        book = [trade]
        with _clean_flags(OPENCLAW_BT_GAP_FILL='open'):
            closed = advance_open_book(book, bars.index[1], by_ticker, bars.loc[:bars.index[1]],
                                       {'state': 'LOW_VOL'}, {'options': {}}, _NoHook(),
                                       dt_priority='stop', counters={})
        assert len(closed) == 1
        assert closed[0]['exit_reason'] == 'stop'
        assert closed[0]['exit_price'] == pytest.approx(95.0)
