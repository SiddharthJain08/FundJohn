"""Exit-hook Phase 1 simulator tests (spec §2). Tasks 2–5 append here."""
from __future__ import annotations

import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import backtest.unified_backtest as ub


class TestBarExit:
    def test_long_stop_only(self):
        assert ub._bar_exit(1, high=101.0, low=94.0, stop_loss=95.0, target_1=108.0, dt_priority='stop') == (95.0, 'stop')

    def test_long_target_only(self):
        assert ub._bar_exit(1, high=109.0, low=99.0, stop_loss=95.0, target_1=108.0, dt_priority='stop') == (108.0, 'target')

    def test_long_neither(self):
        assert ub._bar_exit(1, high=101.0, low=99.0, stop_loss=95.0, target_1=108.0, dt_priority='stop') == (None, None)

    def test_short_mirrors(self):
        assert ub._bar_exit(-1, high=106.0, low=99.0, stop_loss=105.0, target_1=92.0, dt_priority='stop') == (105.0, 'stop')
        assert ub._bar_exit(-1, high=101.0, low=91.0, stop_loss=105.0, target_1=92.0, dt_priority='stop') == (92.0, 'target')

    def test_double_touch_priority(self):
        both = dict(high=110.0, low=90.0, stop_loss=95.0, target_1=108.0)
        assert ub._bar_exit(1, dt_priority='stop', **both) == (95.0, 'stop')
        assert ub._bar_exit(1, dt_priority='target', **both) == (108.0, 'target')


from backtest.open_book import OpenTrade, advance_open_book, resolve_hold_cap


def _bars(rows, dates):
    return pd.DataFrame({'open': [r[0] for r in rows], 'high': [r[1] for r in rows],
                         'low': [r[2] for r in rows], 'close': [r[3] for r in rows]},
                        index=pd.DatetimeIndex(dates, name='date'))


class _Hook:
    """Stand-in strategy: exit_hook=True with a scripted decision."""
    exit_hook = True

    def __init__(self, decide):
        self._decide = decide
        self.calls = []

    def should_exit(self, position, prices, regime, aux_data=None):
        self.calls.append((position['ticker'], prices.index[-1], position['days_held']))
        return self._decide(position, prices)


def _trade(entry_date, hold_cap=21, slippage=0.0, **kw):
    base = dict(ticker='AAA', direction=1, entry_date=entry_date, entry_price=100.0,
                entry_fill=100.0, stop_loss=95.0, target_1=108.0, hold_cap=hold_cap,
                entry_regime='LOW_VOL', signal_params={'k': 1}, slippage=slippage,
                prev_mark=100.0)
    base.update(kw)
    return OpenTrade(**base)


DATES = pd.date_range('2024-01-01', periods=6, freq='B')
CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
CALM = _bars([(c, c + 0.5, c - 0.5, c) for c in CLOSES], DATES)
PANEL = pd.DataFrame({'AAA': CLOSES}, index=DATES)


class TestResolveHoldCap:
    def test_missing_or_invalid_uses_max(self):
        assert resolve_hold_cap(None, 21) == 21
        assert resolve_hold_cap({}, 21) == 21
        assert resolve_hold_cap({'hold_days': 'x'}, 21) == 21
        assert resolve_hold_cap({'hold_days': 0}, 21) == 21

    def test_min_with_max(self):
        assert resolve_hold_cap({'hold_days': 7}, 21) == 7
        assert resolve_hold_cap({'hold_days': 40}, 21) == 21
        assert resolve_hold_cap({'hold_days': 7.9}, 21) == 7


class TestAdvanceOpenBook:
    def _run(self, trade, hook, bars=CALM, dt_priority='stop', dates=None):
        book = [trade]
        closed_all = []
        counters = {}
        for d in (dates if dates is not None else DATES):
            if d <= trade.entry_date:
                continue
            closed = advance_open_book(book, d, {'AAA': bars}, PANEL.loc[:d],
                                       {'state': 'LOW_VOL', 'date': d.date().isoformat()},
                                       {'options': {}}, hook,
                                       dt_priority=dt_priority, counters=counters)
            closed_all.extend(closed)
            if not book:
                break
        return closed_all, book, counters

    def test_hook_exit_at_close_with_adverse_slippage(self):
        hook = _Hook(lambda pos, prices: 'z_revert' if prices.index[-1] == DATES[3] else None)
        closed, book, counters = self._run(_trade(DATES[0], slippage=0.001), hook)
        assert book == [] and len(closed) == 1
        t = closed[0]
        assert t['exit_reason'] == 'strategy_exit:z_revert'
        assert t['exit_date'] == DATES[3].date()
        assert t['exit_price'] == pytest.approx(103.0 * (1 - 0.001))
        assert t['holding_days'] == 3
        assert t['pnl_pct'] == pytest.approx((103.0 * 0.999 - 100.0) / 100.0)
        assert len(t['daily_marks']) == 3
        assert counters['hook_exits'] == 1
        # hook saw days_held 1,2,3 and a panel ending at the evaluation bar
        assert [c[2] for c in hook.calls] == [1, 2, 3]
        assert all(c[1] <= DATES[3] for c in hook.calls)

    def test_bracket_beats_hook_on_same_bar(self):
        rows = [(c, c + 0.5, c - 0.5, c) for c in CLOSES]
        rows[2] = (102.0, 102.5, 94.0, 102.0)          # low pierces the 95 stop on bar 2
        bars = _bars(rows, DATES)
        hook = _Hook(lambda pos, prices: 'z_revert')    # would exit every bar
        closed, book, counters = self._run(_trade(DATES[0]), hook, bars=bars)
        # bar 1 (DATES[1]) has no bracket hit -> hook fires there first
        assert closed[0]['exit_reason'] == 'strategy_exit:z_revert'
        assert closed[0]['exit_date'] == DATES[1].date()
        # now a trade that only becomes hook-eligible on bar 2 loses to the stop
        hook2 = _Hook(lambda pos, prices: 'z_revert' if prices.index[-1] >= DATES[2] else None)
        closed2, _, _ = self._run(_trade(DATES[0]), hook2, bars=bars)
        assert closed2[0]['exit_reason'] == 'stop'
        assert closed2[0]['exit_price'] == pytest.approx(95.0)

    def test_time_cap_from_hold_days(self):
        hook = _Hook(lambda pos, prices: None)
        closed, book, _ = self._run(_trade(DATES[0], hold_cap=2), hook)
        assert closed[0]['exit_reason'] == 'max_hold'
        assert closed[0]['exit_date'] == DATES[2].date()
        assert closed[0]['holding_days'] == 2

    def test_end_of_data_when_bars_run_out(self):
        hook = _Hook(lambda pos, prices: None)
        closed, book, _ = self._run(_trade(DATES[3], hold_cap=21), hook)
        assert closed[0]['exit_reason'] == 'end_of_data'
        assert closed[0]['exit_date'] == DATES[5].date()

    def test_raising_hook_holds_and_counts(self):
        def boom(pos, prices):
            raise RuntimeError('kaboom')
        hook = _Hook(boom)
        closed, book, counters = self._run(_trade(DATES[0], hold_cap=3), hook)
        assert closed[0]['exit_reason'] == 'max_hold'
        assert counters['hook_raised'] == 3
        assert counters['first_hook_raise'].startswith('RuntimeError')

    def test_position_dict_contract(self):
        seen = {}
        def grab(pos, prices):
            seen.update(pos); return 'now'
        hook = _Hook(grab)
        self._run(_trade(DATES[0], signal_params={'pair': 'AAA/BBB'}), hook)
        assert seen == {'ticker': 'AAA', 'direction': 'LONG', 'entry_price': 100.0,
                        'entry_date': DATES[0], 'days_held': 1, 'stop_loss': 95.0,
                        'target_1': 108.0, 'signal_params': {'pair': 'AAA/BBB'}}

    def test_non_hook_instance_skips_hook_entirely(self):
        class NoHook:
            exit_hook = False
            def should_exit(self, *a, **k):
                raise AssertionError('must not be called')
        closed, book, counters = self._run(_trade(DATES[0], hold_cap=2), NoHook())
        assert closed[0]['exit_reason'] == 'max_hold'
        assert counters.get('hook_exits', 0) == 0

    def test_ticker_with_no_bars_after_entry_closes_like_simulate_trade(self):
        # Mirrors simulate_trade's `bars_future.empty` case: no slippage, no mark.
        hook = _Hook(lambda pos, prices: 'z_revert')          # must never be consulted
        only_entry_bar = _bars([(100.0, 100.5, 99.5, 100.0)], DATES[:1])
        closed, book, counters = self._run(_trade(DATES[0], slippage=0.001), hook,
                                           bars=only_entry_bar, dates=DATES[:2])
        assert book == [] and len(closed) == 1
        t = closed[0]
        assert t['exit_reason'] == 'end_of_data'
        assert t['holding_days'] == 0 and t['daily_marks'] == []
        assert t['exit_price'] == 100.0 and t['pnl_pct'] == 0.0
        assert t['exit_date'] == DATES[0].date()
        assert hook.calls == [] and counters.get('hook_exits', 0) == 0
