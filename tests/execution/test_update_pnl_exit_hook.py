# tests/execution/test_update_pnl_exit_hook.py
"""Phase 2 §2.1–§2.6: exit hook + time stop inside engine.update_pnl."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from execution import engine  # noqa: E402
from strategies.base import BaseStrategy, CANONICAL_REGIMES  # noqa: E402

ENV = {'OPENCLAW_EXIT_HOOK_LIVE': '1', 'OPENCLAW_BACKTEST_COUPLED_RECS': '0'}


class _FakeCursor:
    """RealDictCursor stand-in: canned open rows on the status='open' SELECT,
    records every execute with params, fetches [] otherwise."""
    def __init__(self, open_rows):
        self._open_rows = open_rows
        self._fetch = []
        self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._fetch = list(self._open_rows) if "status = 'open'" in sql else []
    def fetchall(self):
        return self._fetch


def _panel(n=10, px=100.0, ticker='AAPL', start='2026-05-04'):
    idx = pd.bdate_range(start, periods=n)          # trading days only
    return pd.DataFrame({ticker: [px] * n}, index=idx)


def _row(**kw):
    base = {'id': 'sig-1', 'strategy_id': 'S_hook', 'ticker': 'AAPL', 'direction': 'LONG',
            'entry_price': 100.0, 'mark_entry_price': 100.5, 'target_date': date(2026, 5, 6),
            'lifecycle_state': 'FILLED', 'stop_loss': 90.0, 'target_1': 120.0,
            'signal_date': date(2026, 5, 5), 'signal_params': {'hold_days': 4, 'k': 1}}
    base.update(kw)
    return base


def _mk(decide, exit_hook=True, sid='S_hook'):
    class H(BaseStrategy):
        id = sid
        active_in_regimes = list(CANONICAL_REGIMES)
        calls = []
        def generate_signals(self, prices, regime, universe, aux_data=None):
            return []
    if exit_hook:
        H.exit_hook = True
        def should_exit(self, position, prices, regime, aux_data=None):
            H.calls.append((position, prices.index[-1], regime))
            return decide(position, prices)
        H.should_exit = should_exit
    return H()


def _closes(cur):
    """(close_reason, close_status) pairs written by the signal_pnl UPSERTs."""
    out = []
    for sql, params in cur.executed:
        if 'INSERT INTO signal_pnl' in sql:
            out.append((params[10], params[7]))
    return out


def test_flag_off_never_calls_hook_and_is_byte_identical():
    strat = _mk(lambda p, x: 'boom')
    cur_on_env_off = _FakeCursor([_row()])
    with patch.dict(os.environ, {**ENV, 'OPENCLAW_EXIT_HOOK_LIVE': '0'}):
        n, closed = engine.update_pnl(cur_on_env_off, _panel(), date(2026, 5, 13),
                                      strategies=[strat], regime={'state': 'LOW_VOL'})
    assert closed == [] and type(strat).calls == []
    assert engine.LAST_EXIT_HOOK_STATS['enabled'] is False
    # kwargs omitted entirely (legacy callers) → same executes
    cur_legacy = _FakeCursor([_row()])
    with patch.dict(os.environ, {**ENV, 'OPENCLAW_EXIT_HOOK_LIVE': '0'}):
        engine.update_pnl(cur_legacy, _panel(), date(2026, 5, 13))
    assert [s for s, _ in cur_legacy.executed] == [s for s, _ in cur_on_env_off.executed]


def test_hook_reason_closes_with_prefixed_reason_and_position_contract():
    seen = {}
    def decide(position, prices):
        seen.update(position); return 'z_revert'
    strat = _mk(decide)
    cur = _FakeCursor([_row()])
    with patch.dict(os.environ, ENV):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13),
                                      strategies=[strat], regime={'state': 'LOW_VOL'}, aux_data={'a': 1})
    assert closed == ['sig-1']
    assert _closes(cur) == [('strategy_exit:z_revert', 'closed')]
    assert any('UPDATE execution_signals SET status' in s for s, _ in cur.executed)
    # spec §1 contract: entry_price = mark, entry_date = target_date, days_held = bars
    assert seen['entry_price'] == 100.5 and seen['entry_date'] == date(2026, 5, 6)
    assert seen['direction'] == 'LONG' and seen['signal_params'] == {'hold_days': 4, 'k': 1}
    assert seen['stop_loss'] == 90.0 and seen['target_1'] == 120.0
    # bars in (2026-05-06, 2026-05-13]: 05-07,08,11,12,13 = 5 (weekend 09/10 excluded)
    assert seen['days_held'] == 5
    assert type(strat).calls[0][2] == {'state': 'LOW_VOL'}
    assert engine.LAST_EXIT_HOOK_STATS['strategy_exit'] == 1


def test_stop_inference_beats_hook():
    strat = _mk(lambda p, x: 'z_revert')
    cur = _FakeCursor([_row()])
    with patch.dict(os.environ, ENV):
        engine.update_pnl(cur, _panel(px=85.0), date(2026, 5, 13), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert _closes(cur) == [('stop_loss', 'closed')]
    assert type(strat).calls == []


def test_raising_hook_holds_and_counts():
    def boom(p, x): raise RuntimeError('kaboom')
    strat = _mk(boom)
    cur = _FakeCursor([_row(signal_params={'hold_days': 30})])
    with patch.dict(os.environ, ENV):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert closed == [] and _closes(cur) == [(None, 'open')]
    assert engine.LAST_EXIT_HOOK_STATS['hook_raised'] == 1
    assert engine.LAST_EXIT_HOOK_STATS['first_hook_raise'].startswith('RuntimeError')


def test_time_stop_from_signal_hold_days():
    strat = _mk(lambda p, x: None)
    cur = _FakeCursor([_row(signal_params={'hold_days': 4})])      # bars_held == 5 >= 4
    with patch.dict(os.environ, ENV):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert closed == ['sig-1'] and _closes(cur) == [('max_hold', 'closed')]
    assert engine.LAST_EXIT_HOOK_STATS['max_hold'] == 1
    # one bar earlier (bars_held == 4) also fires (>=); two earlier (3) holds
    cur2 = _FakeCursor([_row(signal_params={'hold_days': 4})])
    with patch.dict(os.environ, ENV):
        engine.update_pnl(cur2, _panel(), date(2026, 5, 11), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert _closes(cur2) == [(None, 'open')]


def test_time_stop_capped_by_configured_max_hold():
    strat = _mk(lambda p, x: None)
    cur = _FakeCursor([_row(signal_params={'hold_days': 40})])
    with (patch.dict(os.environ, ENV),
          patch('execution.regime_param_resolver.configured_max_hold_days', return_value=5)):
        engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert _closes(cur) == [('max_hold', 'closed')]


def test_non_hook_strategy_and_null_signal_params_untouched():
    plain = _mk(lambda p, x: 'x', exit_hook=False, sid='S_plain')
    cur = _FakeCursor([_row(strategy_id='S_plain', signal_params=None)])
    with patch.dict(os.environ, ENV):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[plain], regime={'state': 'LOW_VOL'})
    assert closed == [] and _closes(cur) == [(None, 'open')]


def test_demoted_strategy_loaded_on_demand():
    hook = _mk(lambda p, x: 'z_revert', sid='S_gone')
    cur = _FakeCursor([_row(strategy_id='S_gone')])
    with (patch.dict(os.environ, ENV),
          patch('strategies.registry.load_strategy_class', return_value=type(hook)) as ld):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[], regime={'state': 'LOW_VOL'})
    assert closed == ['sig-1'] and ld.call_args.args[0] == 'S_gone'
    assert engine.LAST_EXIT_HOOK_STATS['loaded_on_demand'] == 1


def test_bars_held_helper():
    idx = pd.bdate_range('2026-05-04', periods=10)
    p = pd.DataFrame({'AAPL': range(10)}, index=idx)
    assert engine._bars_held(p, date(2026, 5, 6), date(2026, 5, 13)) == 5
    assert engine._bars_held(p, date(2026, 5, 13), date(2026, 5, 13)) == 0
    assert engine._bars_held(pd.DataFrame({'AAPL': [1.0]}), date(2026, 5, 6), date(2026, 5, 13)) is None


def test_exit_hook_run_summary():
    assert engine._exit_hook_run_summary({'enabled': False}) == (None, None)
    line, err = engine._exit_hook_run_summary({'enabled': True, 'strategy_exit': 2, 'max_hold': 1,
                                                'hook_raised': 0, 'first_hook_raise': None,
                                                'loaded_on_demand': 1, 'rows_evaluated': 7})
    assert line == '[exit_hook] closes: 2 strategy_exit, 1 max_hold; hook errors 0; rows 7; instances loaded on demand 1'
    assert err is None
    line, err = engine._exit_hook_run_summary({'enabled': True, 'strategy_exit': 0, 'max_hold': 0,
                                                'hook_raised': 3, 'first_hook_raise': 'ValueError: x',
                                                'loaded_on_demand': 0, 'rows_evaluated': 3})
    assert err == 'exit_hook: 3 hook errors (first: ValueError: x)'


def test_main_passes_context_to_update_pnl():
    """main() must hand strategies/regime/aux_data to update_pnl (source-level
    contract check — main() itself needs a live DB)."""
    import inspect
    src = inspect.getsource(engine.main)
    assert 'update_pnl(cur, prices, run_date,' in src
    assert 'strategies=strategies' in src and 'regime=regime' in src and 'aux_data=aux_data' in src
    assert '_exit_hook_run_summary(' in src
