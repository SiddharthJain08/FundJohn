"""Unit tests for src/execution/activation_apply.py — the daily-cycle
`activation` step. No live DB, no subprocesses: fake conn + fake runner."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from execution import activation_apply as aa  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 8, 17, 4, 0, 2, tzinfo=UTC)    # weekly apply
T1 = dt.datetime(2026, 8, 22, 19, 0, 0, tzinfo=UTC)   # operator moved slider


class FakeCur:
    def __init__(self, rows, raise_on_execute=False):
        self._rows = rows
        self._raise = raise_on_execute

    def execute(self, sql, params=()):
        if self._raise:
            raise RuntimeError('boom')
        self.params = params

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class FakeConn:
    def __init__(self, rows, raise_on_execute=False):
        self._cur = FakeCur(rows, raise_on_execute)
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _marker(ts=T0, threshold=0.5):
    return (aa.MARKER_KEY, json.dumps({'threshold': threshold, 'trigger': 'weekly_cron'}), ts)


# ── pending_state ───────────────────────────────────────────────────────────
def test_not_pending_when_sliders_older_than_marker():
    conn = FakeConn([('strategy_activation_min_sharpe', '0.5', T0 - dt.timedelta(days=3)), _marker(T0)])
    st = aa.pending_state(conn)
    assert st['pending'] is False
    assert st['reasons'] == []
    assert st['marker']['threshold'] == 0.5
    assert st['marker_updated_at'] == T0


def test_pending_when_min_sharpe_newer_than_marker():
    conn = FakeConn([('strategy_activation_min_sharpe', '1', T1), _marker(T0)])
    st = aa.pending_state(conn)
    assert st['pending'] is True
    assert any('strategy_activation_min_sharpe=1' in r for r in st['reasons'])


def test_pending_when_min_trades_newer_than_marker():
    conn = FakeConn([('strategy_activation_min_sharpe', '0.5', T0 - dt.timedelta(days=1)),
                     ('strategy_activation_min_trades', '150', T1), _marker(T0)])
    st = aa.pending_state(conn)
    assert st['pending'] is True
    assert len(st['reasons']) == 1 and 'min_trades=150' in st['reasons'][0]


def test_pending_when_marker_missing():
    conn = FakeConn([('strategy_activation_min_sharpe', '0.5', T0)])
    st = aa.pending_state(conn)
    assert st['pending'] is True
    assert 'missing' in st['reasons'][0]


def test_not_pending_when_no_slider_rows_but_marker_present():
    # Sliders never written ⇒ assigner fail-safes (0.5 / class gate) — the
    # weekly apply already reflects that; nothing to re-apply.
    conn = FakeConn([_marker(T0)])
    assert aa.pending_state(conn)['pending'] is False


def test_read_failure_is_fail_safe_pending():
    conn = FakeConn([], raise_on_execute=True)
    st = aa.pending_state(conn)
    assert st['pending'] is True
    assert conn.rolled_back is True


# ── apply() orchestration ───────────────────────────────────────────────────
class Res:
    def __init__(self, rc):
        self.returncode = rc


def _runner(rcs, calls):
    it = iter(rcs)

    def run(argv, cwd=None, env=None, timeout=None):
        calls.append((argv, env))
        return Res(next(it))
    return run


def test_apply_runs_assigner_then_weights_only_rebuild():
    calls = []
    rc = aa.apply(env={'POSTGRES_URI': 'x', 'OPENCLAW_AUTO_DEMOTE': '1', 'PYTHONPATH': 'extra'},
                  runner=_runner([0, 0], calls))
    assert rc == 0
    assert len(calls) == 2
    a_argv, a_env = calls[0]
    w_argv, w_env = calls[1]
    assert 'backtest.activation_assigner' in a_argv and '--all' in a_argv and '--trigger=daily_cycle' in a_argv
    assert '--dry-run' not in a_argv
    assert 'execution.strategy_weights' in w_argv and '--rebuild' in w_argv and '--trigger=activation_slider' in w_argv
    # weights-only: demote chain forced off for THIS invocation only
    assert w_env['OPENCLAW_AUTO_DEMOTE'] == '0'
    assert a_env['OPENCLAW_AUTO_DEMOTE'] == '1'
    # PYTHONPATH carries ROOT + ROOT/src (+ inherited)
    assert str(aa.ROOT / 'src') in a_env['PYTHONPATH'] and 'extra' in a_env['PYTHONPATH']
    assert a_argv[:3] == ['nice', '-n', '19']


def test_apply_skips_weights_when_assigner_fails():
    calls = []
    rc = aa.apply(env={}, runner=_runner([1], calls))
    assert rc == 1
    assert len(calls) == 1


def test_apply_reports_weights_failure():
    calls = []
    rc = aa.apply(env={}, runner=_runner([0, 2], calls))
    assert rc == 1
    assert len(calls) == 2


# ── main() ──────────────────────────────────────────────────────────────────
def test_main_skips_when_gate_off(capsys):
    calls = []
    rc = aa.main(['--date', '2026-08-24'], env={'OPENCLAW_ACTIVATION_ASSIGNER': '0', 'POSTGRES_URI': 'x'},
                 connect=lambda uri: (_ for _ in ()).throw(AssertionError('must not connect')),
                 runner=_runner([], calls))
    assert rc == 0 and calls == []
    assert 'SKIP' in capsys.readouterr().out


def test_main_no_change_is_noop(capsys):
    calls = []
    conn = FakeConn([('strategy_activation_min_sharpe', '0.5', T0 - dt.timedelta(days=1)), _marker(T0)])
    rc = aa.main(['--date', '2026-08-24'], env={'OPENCLAW_ACTIVATION_ASSIGNER': '1', 'POSTGRES_URI': 'x'},
                 connect=lambda uri: conn, runner=_runner([], calls))
    assert rc == 0 and calls == [] and conn.closed
    assert 'nothing to do' in capsys.readouterr().out


def test_main_pending_applies(capsys):
    calls = []
    conn = FakeConn([('strategy_activation_min_sharpe', '1', T1), _marker(T0)])
    rc = aa.main(['--date', '2026-08-24'], env={'OPENCLAW_ACTIVATION_ASSIGNER': '1', 'POSTGRES_URI': 'x'},
                 connect=lambda uri: conn, runner=_runner([0, 0], calls))
    assert rc == 0 and len(calls) == 2
    out = capsys.readouterr().out
    assert 'PENDING' in out and 'applied' in out


def test_main_dry_run_runs_nothing(capsys):
    calls = []
    conn = FakeConn([('strategy_activation_min_sharpe', '1', T1), _marker(T0)])
    rc = aa.main(['--date', '2026-08-24', '--dry-run'],
                 env={'OPENCLAW_ACTIVATION_ASSIGNER': '1', 'POSTGRES_URI': 'x'},
                 connect=lambda uri: conn, runner=_runner([], calls))
    assert rc == 0 and calls == []
    assert 'dry-run: would run' in capsys.readouterr().out


def test_main_force_applies_without_change():
    calls = []
    conn = FakeConn([('strategy_activation_min_sharpe', '0.5', T0 - dt.timedelta(days=1)), _marker(T0)])
    rc = aa.main(['--force'], env={'OPENCLAW_ACTIVATION_ASSIGNER': '1', 'POSTGRES_URI': 'x'},
                 connect=lambda uri: conn, runner=_runner([0, 0], calls))
    assert rc == 0 and len(calls) == 2


def test_main_failure_is_rc1_never_higher():
    # rc must stay ≤1: daily_cycle_node.js exempts `activation` from abort on
    # rc≠0, but rc≥2 is the documented "always abort" band for every step.
    calls = []
    conn = FakeConn([('strategy_activation_min_sharpe', '1', T1), _marker(T0)])
    rc = aa.main([], env={'OPENCLAW_ACTIVATION_ASSIGNER': '1', 'POSTGRES_URI': 'x'},
                 connect=lambda uri: conn, runner=_runner([137], calls))
    assert rc == 1


def test_main_db_connect_failure_rc1():
    def bad(uri):
        raise RuntimeError('no db')
    rc = aa.main([], env={'OPENCLAW_ACTIVATION_ASSIGNER': '1', 'POSTGRES_URI': 'x'}, connect=bad)
    assert rc == 1
