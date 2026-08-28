# tests/execution/test_exit_hook_live_parity.py
"""Phase 2 §4.1: the live update_pnl hook branch reproduces the backtest
open-book exits (hook exit AND time stop) for LONG and SHORT fixtures.
Backtest side is authoritative (operator ruling 2026-08-07)."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import engine                      # noqa: E402
import backtest.unified_backtest as ub            # noqa: E402
from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES  # noqa: E402

ENV = {'OPENCLAW_EXIT_HOOK_LIVE': '1', 'OPENCLAW_BACKTEST_COUPLED_RECS': '0',
       'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0', 'OPENCLAW_BACKTEST_SLIPPAGE': '0'}
DATES = pd.bdate_range('2026-03-02', periods=40)


def _panel(closes):
    p = pd.DataFrame({'AAA': closes}, index=DATES); p.index.name = 'date'; return p


def _bars(closes):
    return {'AAA': pd.DataFrame({'open': closes, 'high': [c + 0.05 for c in closes],
                                 'low': [c - 0.05 for c in closes], 'close': closes},
                                index=pd.DatetimeIndex(DATES, name='date'))}


def _fixture(direction, exit_level, hold_days):
    """Enters on the first bar with >= 10 prices; exits when the close crosses
    exit_level (long: >=, short: <=) or after hold_days bars."""
    class Fx(BaseStrategy):
        id = 'stub_parity'; min_lookback = 5; active_in_regimes = list(CANONICAL_REGIMES)
        exit_hook = True; fired = False
        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or Fx.fired or not universe: return []
            Fx.fired = True; ep = float(prices['AAA'].iloc[-1])
            sl, t1 = (ep * 0.5, ep * 3.0) if direction == 'LONG' else (ep * 3.0, ep * 0.5)  # brackets never hit
            return [Signal(ticker='AAA', direction=direction, entry_price=ep, stop_loss=sl, target_1=t1,
                           target_2=0.0, target_3=0.0, position_size_pct=0.0, confidence='MED',
                           signal_params={'hold_days': hold_days})]
        def should_exit(self, position, prices, regime, aux_data=None):
            c = float(prices['AAA'].iloc[-1])
            hit = c >= exit_level if position['direction'] == 'LONG' else c <= exit_level
            return 'level' if hit else None
    return Fx


class _FakeCursor:
    def __init__(self, rows): self.rows = rows; self._fetch = []; self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((sql, params)); self._fetch = list(self.rows) if "status = 'open'" in sql else []
    def fetchall(self): return self._fetch


def _backtest_exits(fx_cls, closes):
    inst = fx_cls(); inst.active_in_regimes = list(CANONICAL_REGIMES)
    regimes = pd.Series({d: 'LOW_VOL' for d in DATES})
    with patch.dict(os.environ, ENV):
        out = ub._per_bar_simulate(inst, _panel(closes), _bars(closes), regimes, DATES[0], DATES[-1],
                                   strategy_id='stub_parity', max_hold_days=21, fill_model='same_close')
    t = out['trades'][0]
    return {(t['ticker'], t['exit_date'], t['exit_reason'], t['holding_days'])}, t


def _live_exits(fx_cls, closes, entry_trade, env=ENV):
    """Replay the live branch day by day after the backtest's fill date."""
    fx_cls.fired = True                       # live harness never re-enters
    inst = fx_cls(); panel = _panel(closes)
    entry_date = entry_trade['entry_date']
    row = {'id': 'sig-1', 'strategy_id': 'stub_parity', 'ticker': 'AAA', 'direction': entry_trade['direction'].upper(),
           'entry_price': entry_trade['entry_price'], 'mark_entry_price': entry_trade['entry_price'],
           'target_date': entry_date, 'lifecycle_state': 'FILLED', 'stop_loss': entry_trade['signal_stop'],
           'target_1': entry_trade['signal_target'], 'signal_date': entry_date,
           'signal_params': {'hold_days': 6}}
    for d in DATES[DATES > pd.Timestamp(entry_date)]:
        cur = _FakeCursor([row])
        with patch.dict(os.environ, env):
            n, closed = engine.update_pnl(cur, panel.loc[:d], d.date(), strategies=[inst], regime={'state': 'LOW_VOL'})
        if closed:
            reason = next(p[10] for s, p in cur.executed if 'INSERT INTO signal_pnl' in s)
            bars = engine._bars_held(panel.loc[:d], entry_date, d.date())
            return {('AAA', d.date(), reason, bars)}
    return set()


def _check(direction, closes, exit_level, expect_reason):
    fx = _fixture(direction, exit_level, hold_days=6)
    bt, trade = _backtest_exits(fx, closes)
    live = _live_exits(fx, closes, trade)
    assert live == bt, f'{direction}: live {live} != backtest {bt}'
    assert next(iter(bt))[2] == expect_reason


def test_long_hook_exit_parity():
    closes = [100.0 + i for i in range(40)]                    # rising: LONG hits level 113 on bar 13
    _check('LONG', closes, exit_level=113.0, expect_reason='strategy_exit:level')


def test_short_hook_exit_parity():
    closes = [140.0 - i for i in range(40)]                    # falling: SHORT hits level 127 on bar 13
    _check('SHORT', closes, exit_level=127.0, expect_reason='strategy_exit:level')


def test_time_stop_parity_when_level_never_hit():
    closes = [100.0 + 0.1 * i for i in range(40)]              # never reaches 200 → hold_days=6 time stop
    _check('LONG', closes, exit_level=200.0, expect_reason='max_hold')


def test_flag_off_live_records_no_close():
    fx = _fixture('LONG', 113.0, 6); bt, trade = _backtest_exits(fx, [100.0 + i for i in range(40)])
    assert _live_exits(fx, [100.0 + i for i in range(40)], trade,
                       env={**ENV, 'OPENCLAW_EXIT_HOOK_LIVE': '0'}) == set()
    assert engine.LAST_EXIT_HOOK_STATS['enabled'] is False
