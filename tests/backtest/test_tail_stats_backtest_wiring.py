"""Wiring tests for the P3+R3 additions to unified_backtest.py (2026-08-24):

1. cvar_5 / tail_sortino (migration 148, src/backtest/tail_stats.py) get
   computed and inserted per-regime, WITHOUT disturbing the pre-existing
   sortino/calmar columns (migration 135 — a different, annualized-Sortino
   metric already read by the dashboard; deliberately left untouched, see
   migration 148's header comment).
2. A tail_stats failure is caught and logged (`[tail_stats] skipped: ...`)
   and never fails the backtest.
3. The best-effort per-run tearsheet subprocess call is gated on
   commit=True, OPENCLAW_BT_TEARSHEET != '0', AND generate_tearsheet=True
   (the last one is False ONLY on the --all-live fleet CLI path — review
   finding: firing a tearsheet subprocess per strategy inside the ~140-
   strategy --all-live loop, serialized at 5-180s each with memory-unbounded
   matplotlib/quantstats children, risks the nightly fleet window's real
   bound (RuntimeMaxSec SIGKILL) on this 8GB no-swap box).

No real DB or price-panel IO: load_prices_panels / load_regimes /
load_strategy_class / find_strategy_file / _code_sha / _per_bar_simulate are
mocked (same technique as test_backtest_fill_model.py's _run_capture), and
psycopg2.extras.execute_values is patched to CAPTURE its call args instead of
touching a real DB. subprocess.run is patched too — this suite never spawns
scripts/generate_tearsheet.py as a real process (that script has its own
tests in tests/scripts/test_generate_tearsheet.py).
"""
from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402
from strategies.base import BaseStrategy, CANONICAL_REGIMES  # noqa: E402


class _StubStrategy(BaseStrategy):
    id = 'zzt_stub_tailstats'
    min_lookback = 1
    active_in_regimes = list(CANONICAL_REGIMES)

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return []


def _make_mock_conn():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


def _synthetic_trades(n=25, regime='LOW_VOL', start=date(2024, 1, 2)):
    """n>=20 (default min_obs) trades, all in one regime, mixed pnl_pct so
    downside_dev > 0 (a non-None tail_sortino is possible)."""
    trades = []
    for i in range(n):
        pnl = 3.0 if i % 3 else -4.0
        d = start + timedelta(days=i)
        trades.append({
            'ticker': 'ZZT1', 'direction': 'long',
            'entry_date': d, 'entry_price': 100.0,
            'exit_date': d + timedelta(days=1),
            'exit_price': 100.0 * (1 + pnl / 100.0),
            'exit_reason': 'target', 'pnl_pct': pnl, 'holding_days': 1,
            'entry_regime': regime, 'signal_stop': 95.0, 'signal_target': 105.0,
        })
    return trades


def _sim_result(trades):
    return {
        'trades': trades, 'universe_sizes': [], 'days_processed': len(trades),
        'days_with_signals': len(trades), 'static_universe': ['ZZT1'],
        'min_lookback': 1,
    }


def _regimes_series(regime='LOW_VOL', periods=40):
    idx = pd.date_range('2024-01-01', periods=periods, freq='D')
    return pd.Series([regime] * periods, index=idx)


def _run(trades, *, commit=False, env=None, subprocess_mock=None,
         generate_tearsheet=True):
    """Run unified_backtest.run_backtest with all IO mocked; returns
    (run_id, execute_values_calls, subprocess_mock)."""
    mock_conn = _make_mock_conn()
    close_wide = pd.DataFrame(
        {'ZZT1': [100.0] * 40},
        index=pd.date_range('2024-01-01', periods=40, freq='D'))
    close_wide.index.name = 'date'
    regimes = _regimes_series(periods=40)
    execute_values_calls = []

    def _capture_execute_values(cur, sql, rows, **kw):
        execute_values_calls.append((sql, rows))

    base_env = {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0'}
    base_env.update(env or {})

    if subprocess_mock is None:
        subprocess_mock = MagicMock(
            return_value=MagicMock(returncode=0, stdout='', stderr=''))

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, base_env))
        stack.enter_context(patch('backtest.unified_backtest.load_prices_panels',
                                   return_value=(close_wide, {'ZZT1': close_wide})))
        stack.enter_context(patch('backtest.unified_backtest.load_regimes',
                                   return_value=regimes))
        stack.enter_context(patch('backtest.unified_backtest.load_strategy_class',
                                   return_value=_StubStrategy))
        stack.enter_context(patch(
            'backtest.unified_backtest.find_strategy_file',
            return_value=str(ROOT / 'src/strategies/implementations/momentum_12_1.py')))
        stack.enter_context(patch('backtest.unified_backtest._code_sha', return_value='abc123'))
        stack.enter_context(patch('backtest.unified_backtest._per_bar_simulate',
                                   return_value=_sim_result(trades)))
        stack.enter_context(patch('backtest.unified_backtest.psycopg2.extras.execute_values',
                                   side_effect=_capture_execute_values))
        stack.enter_context(patch('backtest.unified_backtest.subprocess.run', subprocess_mock))
        stack.enter_context(patch('backtest.backtest_panel.rebuild', create=True))
        run_id = ub.run_backtest('zzt_stub_tailstats', conn=mock_conn, commit=commit,
                                  generate_tearsheet=generate_tearsheet)

    return run_id, execute_values_calls, subprocess_mock


def _regimes_insert(execute_values_calls):
    for sql, rows in execute_values_calls:
        if 'strategy_backtest_regimes' in sql:
            return sql, rows
    raise AssertionError('no strategy_backtest_regimes INSERT captured')


def test_cvar_and_tail_sortino_populated_without_disturbing_sortino():
    trades = _synthetic_trades(n=25)
    _, calls, _ = _run(trades, commit=False)
    sql, rows = _regimes_insert(calls)
    assert 'cvar_5' in sql and 'tail_sortino' in sql
    assert 'sortino' in sql and 'calmar' in sql  # pre-existing columns still present

    low_vol_row = next(r for r in rows if r[1] == 'LOW_VOL')
    # tuple layout: run_id, regime, trade_count, sharpe, max_dd_pct,
    # return_pct, hit_rate, avg_pnl_pct, avg_holding_days,
    # oos_days_in_regime, sortino, calmar, cvar_5, tail_sortino
    assert len(low_vol_row) == 14
    existing_sortino, existing_calmar, cvar_5, tail_sortino = low_vol_row[10:14]
    assert cvar_5 is not None
    assert tail_sortino is not None
    # The new tail_sortino is a DIFFERENT metric from the pre-existing
    # (annualized, portfolio-based) sortino column — not overwritten.
    from backtest.tail_stats import sleeve_tail_stats
    pnl_list = [t['pnl_pct'] for t in trades]
    expected = sleeve_tail_stats(pnl_list)
    assert tail_sortino == pytest.approx(expected['sortino'])
    assert cvar_5 == pytest.approx(expected['cvar_5'])

    # A zero-trade regime still gets NULLs for the new columns too.
    empty_row = next(r for r in rows if r[1] != 'LOW_VOL')
    assert empty_row[12] is None and empty_row[13] is None


def test_tail_stats_failure_is_caught_and_logged(capsys):
    trades = _synthetic_trades(n=25)
    with patch('backtest.tail_stats.sleeve_tail_stats', side_effect=RuntimeError('boom')):
        run_id, calls, _ = _run(trades, commit=False)
    # Backtest still completed (run_id returned, regimes row still written).
    assert run_id
    sql, rows = _regimes_insert(calls)
    low_vol_row = next(r for r in rows if r[1] == 'LOW_VOL')
    assert low_vol_row[12] is None and low_vol_row[13] is None  # cvar_5/tail_sortino NULL
    out = capsys.readouterr().out
    assert '[tail_stats] skipped' in out


def test_tearsheet_fires_on_commit_true_default_env():
    trades = _synthetic_trades(n=25)
    run_id, _, sp_mock = _run(trades, commit=True)
    assert sp_mock.call_count == 1
    args, kwargs = sp_mock.call_args
    cmd = args[0]
    assert 'scripts/generate_tearsheet.py' in ' '.join(cmd)
    assert '--run-id' in cmd and run_id in cmd
    assert kwargs.get('timeout') == 180


def test_tearsheet_skipped_when_commit_false():
    trades = _synthetic_trades(n=25)
    _, _, sp_mock = _run(trades, commit=False)
    assert sp_mock.call_count == 0


def test_tearsheet_skipped_when_env_disabled():
    trades = _synthetic_trades(n=25)
    _, _, sp_mock = _run(trades, commit=True, env={'OPENCLAW_BT_TEARSHEET': '0'})
    assert sp_mock.call_count == 0


def test_tearsheet_subprocess_failure_is_swallowed(capsys):
    trades = _synthetic_trades(n=25)
    sp_mock = MagicMock(side_effect=TimeoutError('wedged'))
    run_id, _, _ = _run(trades, commit=True, subprocess_mock=sp_mock)
    assert run_id  # run_backtest did not raise
    out = capsys.readouterr().out
    assert '[tearsheet] skipped' in out


# ── fleet-scale cost fix: --all-live must never fire the subprocess ────────
# (review finding: the tearsheet subprocess was firing per strategy inside
# the --all-live loop too — ~140 strategies x 5-180s serialized children
# endangers the nightly RuntimeMaxSec SIGKILL bound on this 8GB no-swap box.)

def test_generate_tearsheet_false_suppresses_subprocess_even_when_commit_true():
    """Direct run_backtest-level check of the new gate: generate_tearsheet=
    False (what --all-live now passes) must suppress the subprocess even
    though commit=True and OPENCLAW_BT_TEARSHEET is left at its ON default."""
    trades = _synthetic_trades(n=25)
    _, _, sp_mock = _run(trades, commit=True, generate_tearsheet=False)
    assert sp_mock.call_count == 0


def test_main_all_live_cli_passes_generate_tearsheet_false(monkeypatch):
    """CLI-level: `--all-live` must wire generate_tearsheet=False into every
    run_backtest() call, not just default to it by accident."""
    seen = []
    monkeypatch.setattr(ub, '_all_live_strategies', lambda: ['S_a', 'S_b'])
    monkeypatch.setattr(ub, 'run_backtest',
                         lambda sid, **kw: seen.append((sid, kw.get('generate_tearsheet'))) or 'run-id')
    monkeypatch.setattr(sys, 'argv', ['prog', '--all-live'])
    assert ub.main() == 0
    assert seen == [('S_a', False), ('S_b', False)]


def test_main_strategy_id_cli_defaults_generate_tearsheet_true(monkeypatch):
    """CLI-level: the single-strategy path must NOT pass generate_tearsheet=
    False — it relies on run_backtest's True default, so the tearsheet
    subprocess still fires for --strategy-id / --strategy-file invocations."""
    captured = {}
    monkeypatch.setattr(
        ub, 'run_backtest',
        lambda sid, **kw: captured.update({'sid': sid, **kw}) or 'run-id')
    monkeypatch.setattr(ub, '_resolve_instrument_class', lambda sid, filepath=None: 'equity')
    monkeypatch.setattr(sys, 'argv', ['prog', '--strategy-id', 'S_solo'])
    assert ub.main() == 0
    assert captured['sid'] == 'S_solo'
    assert captured.get('generate_tearsheet', True) is True
