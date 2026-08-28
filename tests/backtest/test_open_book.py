"""Exit-hook Phase 1 simulator tests (spec §2). Tasks 2–5 append here."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
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
        # spec §1: position['entry_price'] is the ACTUAL FILL (entry_fill),
        # i.e. the raw level after adverse entry slippage -- the same quantity
        # the live mirror will pass as mark_entry_price. A hook that computes
        # a return from entry must see what was paid, not the signal level.
        seen = {}
        def grab(pos, prices):
            seen.update(pos); return 'now'
        hook = _Hook(grab)
        self._run(_trade(DATES[0], slippage=0.001, entry_fill=100.0 * 1.001,
                          signal_params={'pair': 'AAA/BBB'}), hook)
        assert seen.pop('entry_price') == pytest.approx(100.0 * 1.001)
        assert seen == {'ticker': 'AAA', 'direction': 'LONG',
                        'entry_date': DATES[0], 'days_held': 1, 'stop_loss': 95.0,
                        'target_1': 108.0, 'signal_params': {'pair': 'AAA/BBB'}}

    def test_prev_mark_defaults_to_entry_fill(self):
        # An OpenTrade built without an explicit prev_mark must mark its first
        # interior bar off the fill, not off 0.0 (which would divide by zero).
        t = OpenTrade(ticker='AAA', direction=1, entry_date=DATES[0], entry_price=100.0,
                      entry_fill=100.1, stop_loss=95.0, target_1=108.0, hold_cap=21,
                      entry_regime='LOW_VOL', signal_params={}, slippage=0.001)
        assert t.prev_mark == 100.1
        # an explicitly supplied prev_mark is never overwritten
        assert _trade(DATES[0]).prev_mark == 100.0

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


from tests.backtest.test_backtest_fill_model import (_bars_from_rows, _run_capture,
                                                    _trivial_dataset)
from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES


def _mk_hook_cls(decide, hold_days=None, stop_pct=0.07, target_pct=0.08):
    """One LONG signal on the first bar with len(prices) >= 10, then quiet."""
    class HookStub(BaseStrategy):
        id = 'stub_hook'
        min_lookback = 5
        active_in_regimes = list(CANONICAL_REGIMES)
        exit_hook = True
        fired = False

        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or HookStub.fired or not universe:
                return []
            HookStub.fired = True
            t = universe[0]
            ep = float(prices[t].iloc[-1])
            sp = {'hold_days': hold_days} if hold_days else {}
            return [Signal(ticker=t, direction='LONG', entry_price=ep,
                           stop_loss=ep * (1 - stop_pct), target_1=ep * (1 + target_pct),
                           target_2=0.0, target_3=0.0, position_size_pct=0.0,
                           confidence='MED', signal_params=sp)]

        def should_exit(self, position, prices, regime, aux_data=None):
            return decide(position, prices)
    return HookStub


def _mk_plain_cls(stop_pct=0.07, target_pct=0.08):
    class PlainStub(BaseStrategy):
        id = 'stub_plain'
        min_lookback = 5
        active_in_regimes = list(CANONICAL_REGIMES)
        fired = False

        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or PlainStub.fired or not universe:
                return []
            PlainStub.fired = True
            t = universe[0]
            ep = float(prices[t].iloc[-1])
            return [Signal(ticker=t, direction='LONG', entry_price=ep,
                           stop_loss=ep * (1 - stop_pct), target_1=ep * (1 + target_pct),
                           target_2=0.0, target_3=0.0, position_size_pct=0.0, confidence='MED')]
    return PlainStub


def _dataset(n=30):
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    closes = [100.0 + 0.5 * i for i in range(n)]
    close_wide = pd.DataFrame({'AAA': closes}, index=dates); close_wide.index.name = 'date'
    bars = _bars_from_rows({'AAA': [(c - 0.1, c + 0.2, c - 0.2, c) for c in closes]}, dates)
    regimes = pd.Series({d: 'LOW_VOL' for d in dates})
    return close_wide, bars, regimes, dates, closes


class TestPerBarSimulateOpenBook:
    def _strip(self, trades):
        return [{k: v for k, v in t.items() if k != 'daily_marks'} for t in trades]

    def test_hook_never_firing_equals_simulate_trade_path(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        plain = _run_capture(_mk_plain_cls(), close_wide, bars, regimes, fill_model='same_close')
        hook = _run_capture(_mk_hook_cls(lambda p, x: None), close_wide, bars, regimes, fill_model='same_close')
        assert plain and len(plain) == 1
        assert self._strip(hook) == self._strip(plain)
        assert [round(m[1], 12) for m in hook[0]['daily_marks']] == [round(m[1], 12) for m in plain[0]['daily_marks']]

    def test_hook_exit_lands_in_trade_list(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        entry_idx = 9                                   # first bar with len(prices) >= 10
        exit_day = dates[entry_idx + 4]
        cls = _mk_hook_cls(lambda p, prices: 'z_revert' if prices.index[-1] == exit_day else None)
        trades = _run_capture(cls, close_wide, bars, regimes, fill_model='same_close')
        assert len(trades) == 1
        t = trades[0]
        assert t['exit_reason'] == 'strategy_exit:z_revert'
        assert t['exit_date'] == exit_day.date()
        assert t['holding_days'] == 4
        # flat adverse slippage may still apply under _run_capture (spread costs off,
        # OPENCLAW_BACKTEST_SLIPPAGE default ON) -> compare within 30 bps of the close
        assert abs(t['exit_price'] / closes[entry_idx + 4] - 1.0) < 0.003

    def test_signal_hold_days_caps_hold(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        cls = _mk_hook_cls(lambda p, x: None, hold_days=3)
        trades = _run_capture(cls, close_wide, bars, regimes, fill_model='same_close')
        assert trades[0]['exit_reason'] == 'max_hold' and trades[0]['holding_days'] == 3

    def test_open_fill_model_rejected_for_hook_strategies(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        with pytest.raises(ValueError, match='exit_hook'):
            _run_capture(_mk_hook_cls(lambda p, x: None), close_wide, bars, regimes, fill_model='open')

    def test_trade_open_at_window_end_drains_past_end_dt(self):
        close_wide, bars, regimes, dates, closes = _dataset(n=40)
        cls = _mk_hook_cls(lambda p, x: None, hold_days=5)
        inst = cls(); inst.active_in_regimes = list(CANONICAL_REGIMES)
        # entry lands on dates[9]; end the OOS window on dates[11]: the trade
        # must still run to its 5-bar cap on dates[14] like simulate_trade would
        # (simulate_trade walks bars_by_ticker past end_dt; the open book drains).
        with patch.dict(os.environ, {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0'}):
            out = ub._per_bar_simulate(inst, close_wide, bars, regimes, dates[0], dates[11],
                                       strategy_id='stub_hook', max_hold_days=21,
                                       fill_model='same_close')
        t = out['trades'][0]
        assert t['exit_reason'] == 'max_hold' and t['exit_date'] == dates[14].date()
        assert out['hook_exits'] == 0

    def test_drain_path_passes_real_aux_to_hook(self):
        close_wide, bars, regimes, dates, closes = _dataset(n=40)
        seen = []

        class AuxHook(BaseStrategy):
            id = 'stub_aux'
            min_lookback = 5
            active_in_regimes = list(CANONICAL_REGIMES)
            exit_hook = True
            fired = False

            def generate_signals(self, prices, regime, universe, aux_data=None):
                if len(prices) < 10 or AuxHook.fired or not universe:
                    return []
                AuxHook.fired = True
                t = universe[0]
                ep = float(prices[t].iloc[-1])
                return [Signal(ticker=t, direction='LONG', entry_price=ep,
                               stop_loss=ep * 0.93, target_1=ep * 1.5,
                               target_2=0.0, target_3=0.0, position_size_pct=0.0,
                               confidence='MED', signal_params={'hold_days': 8})]

            def should_exit(self, position, prices, regime, aux_data=None):
                seen.append((prices.index[-1], bool((aux_data or {}).get('marker'))))
                return None

        inst = AuxHook(); inst.active_in_regimes = list(CANONICAL_REGIMES)
        fake_aux = lambda date, **kw: {'options': {}, 'marker': True}
        with (
            patch.dict(os.environ, {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0'}),
            patch('strategies.aux_data_loader.load_aux_data', side_effect=fake_aux),
        ):
            out = ub._per_bar_simulate(inst, close_wide, bars, regimes, dates[0], dates[11],
                                       strategy_id='stub_aux', max_hold_days=21,
                                       fill_model='same_close')
        # entry on dates[9]; hook consulted dates[10..17]; bars after end_dt=dates[11] are the drain
        assert out['trades'][0]['exit_reason'] == 'max_hold'
        drained = [m for d, m in seen if d > dates[11]]
        assert len(drained) >= 5, seen
        assert all(drained), seen           # RED before the fix: drained marks are all False
        assert all(m for d, m in seen), seen


GOLDEN_PATH = (Path(__file__).resolve().parent / 'fixtures' / 'open_book_identity_golden.json')


def test_plain_strategy_matches_pre_phase1_golden():
    """Spec §2's core promise: a strategy WITHOUT exit_hook produces the same
    trade list after Phase 1 as before it. The fixture was generated by the
    PRE-Phase-1 engine (commit 4af0086) in a read-only worktree, through this
    same `_run_capture` path — so this is a cross-COMMIT identity check, not a
    same-process comparison of two code paths (which is what
    test_hook_never_firing_equals_simulate_trade_path already covers).

    Regenerating it against the current engine would defeat the point: if this
    test goes red, the non-hook path moved and that is the finding.
    """
    golden = json.loads(GOLDEN_PATH.read_text())
    assert golden['generated_from_commit'] == '4af0086'

    close_wide, bars, regimes, dates, closes = _dataset()
    trades = _run_capture(_mk_plain_cls(), close_wide, bars, regimes, fill_model='same_close')

    assert len(trades) == len(golden['trades'])
    for got, want in zip(trades, golden['trades']):
        assert set(got) == set(want), (set(got) ^ set(want))
        for key, wv in want.items():
            gv = got[key]
            if key == 'daily_marks':
                assert len(gv) == len(wv), key
                for (gd, gm), (wd, wm) in zip(gv, wv):
                    assert pd.Timestamp(gd).date().isoformat() == wd
                    assert math.isclose(float(gm), float(wm), rel_tol=1e-12, abs_tol=0.0)
            elif key in ('entry_date', 'exit_date'):
                assert pd.Timestamp(gv).date().isoformat() == wv, key
            elif isinstance(wv, str) and not isinstance(gv, str):
                assert math.isclose(float(gv), float(wv), rel_tol=1e-12, abs_tol=0.0), key
            else:
                assert gv == wv, key


class TestConfigJsonExitHook:
    def _config_json_of(self, strategy_cls):
        close_wide, bars, regimes, dates, closes = _dataset()
        import json
        from unittest.mock import MagicMock
        seen = {}
        mock_conn = MagicMock(); mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = lambda s: s; mock_conn.__exit__ = MagicMock(return_value=False)
        def exec_spy(sql, params=None):
            if 'INSERT INTO strategy_backtest_runs' in str(sql):
                seen['params'] = params
        mock_cur.execute.side_effect = exec_spy
        with (
            patch.dict(os.environ, {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0',
                                     'OPENCLAW_BACKTEST_COUPLED_RECS': '0'}),
            patch('backtest.unified_backtest.load_prices_panels', return_value=(close_wide, bars)),
            patch('backtest.unified_backtest.load_regimes', return_value=regimes),
            patch('backtest.unified_backtest.load_strategy_class', return_value=strategy_cls),
            patch('backtest.unified_backtest.find_strategy_file', return_value='x.py'),
            patch('backtest.unified_backtest._code_sha', return_value='abc123'),
            patch('backtest.unified_backtest.psycopg2.extras.execute_values'),
        ):
            # commit=False: the runs INSERT is still executed on the mocked cursor and
            # simply never committed (nothing rolls back on this clean path); the
            # `if commit:` panel rebuild — which would touch the real DB — is skipped.
            # max_hold_days=21 pinned explicitly, and OPENCLAW_BACKTEST_COUPLED_RECS
            # forced off above: both belt-and-braces against _configured_max_hold_days
            # reaching through regime_param_resolver to its OWN psycopg2.connect(...)
            # (bypassing mock_conn) when the coupled-recs gate is on in the environment.
            ub.run_backtest(strategy_cls.id, conn=mock_conn, commit=False,
                             fill_model='same_close', max_hold_days=21)
        cfg = next(p for p in seen['params'] if isinstance(p, str) and p.startswith('{') and 'max_hold_days' in p)
        return json.loads(cfg)

    def test_plain_strategy_records_false(self):
        cfg = self._config_json_of(_mk_plain_cls())
        assert cfg['exit_hook'] is False and cfg['hook_exits'] == 0
        assert cfg['hook_raised'] == 0

    def test_hook_strategy_records_true_and_count(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        cls = _mk_hook_cls(lambda p, prices: 'z_revert' if p['days_held'] == 2 else None)
        cfg = self._config_json_of(cls)
        assert cfg['exit_hook'] is True and cfg['hook_exits'] == 1
        assert cfg['hook_raised'] == 0

    def test_hook_errors_are_persisted_not_just_logged(self):
        # A run whose hook raised on every bar produces the same trade list as
        # a run whose hook never fired (spec §1: raise => hold). Without
        # hook_raised in config_json the two are indistinguishable after the
        # fact -- the counter only ever reached the journal.
        def boom(position, prices):
            raise RuntimeError('kaboom')
        cfg = self._config_json_of(_mk_hook_cls(boom))
        assert cfg['exit_hook'] is True and cfg['hook_exits'] == 0
        assert cfg['hook_raised'] > 0


def _regime_payload_for(state, cur_d):
    return {'state': (str(state) if state is not None else None), 'date': cur_d.isoformat(),
            'one_hot': {r: (1.0 if r == state else 0.0) for r in CANONICAL_REGIMES},
            'transition_probs': {r1: {r2: (1.0 if r1 == r2 else 0.0) for r2 in CANONICAL_REGIMES}
                                 for r1 in CANONICAL_REGIMES}}


class TestPhase2Residuals:
    def test_hook_receives_full_regime_payload(self):
        close_wide, bars, regimes, dates, closes = _dataset(n=30)
        seen = []
        cls = _mk_hook_cls(lambda p, x: None, hold_days=3)
        orig = cls.should_exit
        def spy(self, position, prices, regime, aux_data=None):
            seen.append(regime); return orig(self, position, prices, regime, aux_data)
        cls.should_exit = spy
        inst = cls(); inst.active_in_regimes = list(CANONICAL_REGIMES)
        with patch.dict(os.environ, {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0',
                                     'OPENCLAW_BACKTEST_COUPLED_RECS': '0'}):
            ub._per_bar_simulate(inst, close_wide, bars, regimes, dates[0], dates[-1],
                                 strategy_id='stub_hook', max_hold_days=21, fill_model='same_close')
        assert seen, 'hook never consulted'
        assert seen[0] == _regime_payload_for('LOW_VOL', dates[10].date())
        assert all(set(r) == {'state', 'date', 'one_hot', 'transition_probs'} for r in seen)

    def test_short_trade_end_to_end_matches_simulate_trade(self):
        close_wide, bars, regimes, dates, closes = _dataset(n=30)
        # a falling tape so a SHORT is the natural trade: reverse the closes
        rev = list(reversed(closes))
        close_wide = pd.DataFrame({'AAA': rev}, index=dates); close_wide.index.name = 'date'
        bars = _bars_from_rows({'AAA': [(c - 0.1, c + 0.2, c - 0.2, c) for c in rev]}, dates)

        def mk(hook):
            class S(BaseStrategy):
                id = 'stub_short'; min_lookback = 5; active_in_regimes = list(CANONICAL_REGIMES); fired = False
                exit_hook = hook
                def generate_signals(self, prices, regime, universe, aux_data=None):
                    if len(prices) < 10 or S.fired or not universe: return []
                    S.fired = True; t = universe[0]; ep = float(prices[t].iloc[-1])
                    return [Signal(ticker=t, direction='SHORT', entry_price=ep, stop_loss=ep * 1.07,
                                   target_1=ep * 0.92, target_2=0.0, target_3=0.0,
                                   position_size_pct=0.0, confidence='MED')]
                if hook:
                    def should_exit(self, position, prices, regime, aux_data=None): return None
            return S
        plain = _run_capture(mk(False), close_wide, bars, regimes, fill_model='same_close')
        hooked = _run_capture(mk(True), close_wide, bars, regimes, fill_model='same_close')
        assert plain and plain[0]['direction'] == 'short'
        strip = lambda ts: [{k: v for k, v in t.items() if k != 'daily_marks'} for t in ts]
        assert strip(hooked) == strip(plain)
        assert [round(m[1], 12) for m in hooked[0]['daily_marks']] == [round(m[1], 12) for m in plain[0]['daily_marks']]
