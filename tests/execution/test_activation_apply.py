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
    def __init__(self, rows, raise_on_execute=False, newest_run=None,
                sleeve_ids=('S_beta_spy',), sleeve_run_id=None,
                raise_on_registry=False):
        self._rows = rows
        self._raise = raise_on_execute
        self._newest_run = newest_run
        # sleeve_ids/sleeve_run_id back the bench-sleeve re-apply trigger
        # (Task 2, spec §2): resolve_bench_sleeve_id's registry lookup
        # (`strategy_registry` in the SQL) and activation_apply's own
        # "latest primary_window run_id for that sleeve" query
        # (`SELECT run_id FROM strategy_backtest_runs`, distinct from the
        # pre-existing `MAX(r.run_at)` staleness probe below even though
        # both touch strategy_backtest_runs). Defaults reproduce the
        # pre-Task-2 fixture shape exactly: a resolvable sleeve id, but NO
        # sleeve run found (sleeve_run_id=None) -- so this new check stays
        # silent (bench_run_id ends up None) unless a test opts in.
        self._sleeve_ids = sleeve_ids
        self._sleeve_run_id = sleeve_run_id
        # raise_on_registry: fail ONLY the registry lookup inside
        # resolve_bench_sleeve_id (scoped, unlike raise_on_execute which
        # fails every query) -- exercises _bench_sleeve_run_id's own
        # fail-safe (returns None, does not force pending on its own)
        # without also tripping the pipeline_config read's fail-safe.
        self._raise_on_registry = raise_on_registry
        self._last_sql = ''

    def execute(self, sql, params=()):
        if self._raise:
            raise RuntimeError('boom')
        if self._raise_on_registry and 'strategy_registry' in sql:
            self._last_sql = sql
            raise RuntimeError('registry down')
        self._last_sql = sql
        self.params = params

    def fetchall(self):
        if 'strategy_registry' in self._last_sql:
            return [(sid,) for sid in self._sleeve_ids]
        return list(self._rows)

    def fetchone(self):
        # The 2026-09-08 staleness probe (MAX(run_at) over primary runs).
        if 'MAX(r.run_at)' in self._last_sql:
            return (self._newest_run,)
        # The bench-sleeve's own latest primary_window run_id (Task 2).
        if 'SELECT run_id FROM strategy_backtest_runs' in self._last_sql:
            return (self._sleeve_run_id,)
        return None

    def close(self):
        pass


class FakeConn:
    def __init__(self, rows, raise_on_execute=False, newest_run=None,
                sleeve_ids=('S_beta_spy',), sleeve_run_id=None,
                raise_on_registry=False):
        self._cur = FakeCur(rows, raise_on_execute, newest_run=newest_run,
                            sleeve_ids=sleeve_ids, sleeve_run_id=sleeve_run_id,
                            raise_on_registry=raise_on_registry)
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _marker(ts=T0, threshold=0.5, bench_run_id=None):
    payload = {'threshold': threshold, 'trigger': 'weekly_cron'}
    if bench_run_id is not None:
        payload['bench_run_id'] = bench_run_id
    return (aa.MARKER_KEY, json.dumps(payload), ts)


# ── pending_state ───────────────────────────────────────────────────────────
def test_not_pending_when_sliders_older_than_marker():
    conn = FakeConn([('strategy_activation_min_sharpe', '0.5', T0 - dt.timedelta(days=3)), _marker(T0)])
    st = aa.pending_state(conn)
    assert st['pending'] is False
    assert st['reasons'] == []
    assert st['marker']['threshold'] == 0.5
    assert st['marker_updated_at'] == T0


def test_min_sharpe_row_change_no_longer_triggers_pending():
    # Task 2 (spec 2026-09-25-activation-bench-relative §3): the min-Sharpe
    # slider is REMOVED from SLIDER_KEYS -- eligibility no longer reads it
    # at all, so a fresh min-Sharpe row (even newer than the marker) must
    # NOT mark the step pending. Contrast with min-trades below, which
    # still does (the min-TRADES slider stays).
    conn = FakeConn([('strategy_activation_min_sharpe', '1', T1), _marker(T0)])
    st = aa.pending_state(conn)
    assert st['pending'] is False
    assert st['sliders'] == {}
    assert not any('min_sharpe' in r for r in st['reasons'])


def test_pending_when_min_trades_newer_than_marker():
    conn = FakeConn([('strategy_activation_min_sharpe', '0.5', T0 - dt.timedelta(days=1)),
                     ('strategy_activation_min_trades', '150', T1), _marker(T0)])
    st = aa.pending_state(conn)
    assert st['pending'] is True
    assert len(st['reasons']) == 1 and 'min_trades=150' in st['reasons'][0]


def test_pending_when_excess_newer_than_marker():
    # Amendment 1 §8 / Task 4: the EXCESS slider (strategy_activation_
    # excess_sharpe) is a NEW SLIDER_KEYS member -- a fresh row newer than
    # the marker marks the step pending, exactly like min-trades above.
    conn = FakeConn([('strategy_activation_excess_sharpe', '0.30', T1), _marker(T0)])
    st = aa.pending_state(conn)
    assert st['pending'] is True
    assert len(st['reasons']) == 1 and 'strategy_activation_excess_sharpe=0.30' in st['reasons'][0]


def test_not_pending_when_excess_older_than_marker():
    conn = FakeConn([('strategy_activation_excess_sharpe', '0.30', T0 - dt.timedelta(days=1)),
                     _marker(T0)])
    st = aa.pending_state(conn)
    assert st['pending'] is False


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


def test_pending_when_primary_run_lands_after_marker():
    # 2026-09-08: the fleet epoch re-backtests nightly; a run landing after
    # the marker means eligibility was derived from superseded sleeves
    # (S_ma_tsmom_crossover sat eligible with all-negative fresh sleeves).
    conn = FakeConn([('strategy_activation_min_sharpe', '1', T0 - dt.timedelta(days=1)),
                     _marker(T0)], newest_run=T0 + dt.timedelta(hours=5))
    st = aa.pending_state(conn)
    assert st['pending'] is True
    assert any('primary backtest run landed' in r for r in st['reasons'])


def test_not_pending_when_runs_older_than_marker():
    conn = FakeConn([('strategy_activation_min_sharpe', '1', T0 - dt.timedelta(days=1)),
                     _marker(T0)], newest_run=T0 - dt.timedelta(days=2))
    st = aa.pending_state(conn)
    assert st['pending'] is False
    assert st['newest_primary_run_at'] == T0 - dt.timedelta(days=2)


# ── benchmark-sleeve re-apply trigger (Task 2, spec §2) ─────────────────────
def test_pending_when_bench_sleeve_run_id_differs_from_marker():
    # A fresh S_beta_spy primary backtest landed since the marker was
    # stamped -- eligibility was derived from a stale bench comparator.
    conn = FakeConn([_marker(T0, bench_run_id='r-old')],
                    newest_run=T0 - dt.timedelta(days=2),   # no OTHER staleness reason
                    sleeve_run_id='r-new')
    st = aa.pending_state(conn)
    assert st['pending'] is True
    assert st['bench_run_id'] == 'r-new'
    assert any("benchmark sleeve primary run 'r-new'" in r for r in st['reasons'])


def test_not_pending_when_bench_sleeve_run_id_matches_marker():
    conn = FakeConn([_marker(T0, bench_run_id='r-same')],
                    newest_run=T0 - dt.timedelta(days=2),
                    sleeve_run_id='r-same')
    st = aa.pending_state(conn)
    assert st['pending'] is False
    assert st['bench_run_id'] == 'r-same'


def test_pending_when_marker_has_no_bench_run_id_but_a_sleeve_run_exists():
    # Pre-Task-1 marker (or a sleeve lookup that failed at stamp time): no
    # `bench_run_id` recorded at all. A missing field must count as
    # pending, not be silently trusted as "nothing changed".
    conn = FakeConn([_marker(T0)],   # no bench_run_id key in the payload
                    newest_run=T0 - dt.timedelta(days=2),
                    sleeve_run_id='r-new')
    st = aa.pending_state(conn)
    assert st['pending'] is True
    assert any('bench_run_id None' in r for r in st['reasons'])


def test_bench_sleeve_registry_lookup_failure_is_not_pending_on_its_own():
    # A broken registry read (resolve_bench_sleeve_id's own scoped
    # fail-safe: falls back to the literal sleeve id, rolls back, keeps
    # going -- see backtest.activation_assigner.resolve_bench_sleeve_id)
    # must not force a re-apply every cycle forever. With no sleeve run
    # found under the fallback id either (sleeve_run_id=None, the default),
    # bench_run_id resolves to None and this check contributes no reason --
    # the general newest-run-approved-strategies staleness check already
    # covers the fail-toward-pending case for a broken runs-table read.
    conn = FakeConn([_marker(T0)], newest_run=T0 - dt.timedelta(days=2),
                    raise_on_registry=True)
    st = aa.pending_state(conn)
    assert st['pending'] is False
    assert st['bench_run_id'] is None
    assert conn.rolled_back is True   # resolve_bench_sleeve_id's own rollback ran


def test_bench_sleeve_run_id_query_ties_break_deterministically():
    # F3 (fix round 1): two sleeve primary_window runs stamped at the
    # identical run_at (a backfill, or a fast rerun in the same second)
    # must resolve to the same row every time this query runs -- the fake
    # doesn't evaluate ORDER BY itself (FakeCur just returns the canned
    # response), so this asserts the SQL TEXT carries the tie-break,
    # matching backtest.activation_assigner.load_bench_sharpe's identical
    # query shape (same "sleeve's latest primary_window run" intent) so the
    # two never disagree about which run is "latest" on a tied timestamp.
    conn = FakeConn([], sleeve_run_id='r9')
    run_id = aa._bench_sleeve_run_id(conn)
    assert run_id == 'r9'
    sql = conn._cur._last_sql
    assert 'SELECT run_id FROM strategy_backtest_runs' in sql
    assert 'ORDER BY run_at DESC, run_id DESC' in sql


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
    assert 'execution.strategy_weights' in w_argv and '--rebuild' in w_argv and '--trigger=activation_bench' in w_argv
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
    # strategy_activation_min_trades (the slider that STAYS) newer than the
    # marker -- min_sharpe would no longer trigger this (see
    # test_min_sharpe_row_change_no_longer_triggers_pending above).
    calls = []
    conn = FakeConn([('strategy_activation_min_trades', '150', T1), _marker(T0)])
    rc = aa.main(['--date', '2026-08-24'], env={'OPENCLAW_ACTIVATION_ASSIGNER': '1', 'POSTGRES_URI': 'x'},
                 connect=lambda uri: conn, runner=_runner([0, 0], calls))
    assert rc == 0 and len(calls) == 2
    out = capsys.readouterr().out
    assert 'PENDING' in out and 'applied' in out


def test_main_dry_run_runs_nothing(capsys):
    calls = []
    conn = FakeConn([('strategy_activation_min_trades', '150', T1), _marker(T0)])
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
    conn = FakeConn([('strategy_activation_min_trades', '150', T1), _marker(T0)])
    rc = aa.main([], env={'OPENCLAW_ACTIVATION_ASSIGNER': '1', 'POSTGRES_URI': 'x'},
                 connect=lambda uri: conn, runner=_runner([137], calls))
    assert rc == 1


def test_main_db_connect_failure_rc1():
    def bad(uri):
        raise RuntimeError('no db')
    rc = aa.main([], env={'OPENCLAW_ACTIVATION_ASSIGNER': '1', 'POSTGRES_URI': 'x'}, connect=bad)
    assert rc == 1
