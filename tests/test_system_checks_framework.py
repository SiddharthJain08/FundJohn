"""Framework-level tests for system_checks. Each check has its own integration
behavior (against live DB/broker), but the registry + runner + CLI must work
purely offline."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import system_checks.registry as registry_mod
from system_checks.registry import check
from system_checks.runner import run_one, run_all, summarize
from system_checks.types import Status, CheckResult
from system_checks import cli


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    """Each test gets a fresh registry. Saves/restores the production one."""
    saved = dict(registry_mod._REGISTRY)
    registry_mod._REGISTRY.clear()
    yield
    registry_mod._REGISTRY.clear()
    registry_mod._REGISTRY.update(saved)


def test_register_and_run_passing_check():
    @check(name='ok', tags=['unit'], requires=['fs'])
    def _():
        return Status.PASS, 'fine'

    r = run_one('ok')
    assert r.status is Status.PASS
    assert r.detail == 'fine'
    assert r.tags == ['unit']
    assert r.elapsed_ms >= 0


def test_run_one_warn_and_fail_distinct():
    @check(name='warn', tags=['unit'], requires=['fs'])
    def _warn():
        return Status.WARN, 'caution'

    @check(name='fail', tags=['unit'], requires=['fs'])
    def _fail():
        return Status.FAIL, 'broken'

    assert run_one('warn').status is Status.WARN
    assert run_one('fail').status is Status.FAIL


def test_unhandled_exception_becomes_error_not_crash():
    @check(name='boom', tags=['unit'], requires=['fs'])
    def _():
        raise RuntimeError('uh oh')

    r = run_one('boom')
    assert r.status is Status.ERROR
    assert 'uh oh' in r.detail
    assert r.error is not None  # traceback captured


def test_check_returning_non_status_becomes_error():
    @check(name='bad_return', tags=['unit'], requires=['fs'])
    def _():
        return 'pass', 'oops'  # bare string, not Status

    r = run_one('bad_return')
    assert r.status is Status.ERROR
    assert 'non-Status' in r.detail


def test_missing_dep_skips(monkeypatch):
    monkeypatch.delenv('POSTGRES_URI', raising=False)

    @check(name='needs_db', tags=['unit'], requires=['db'])
    def _():
        return Status.PASS, 'unreachable'

    r = run_one('needs_db')
    assert r.status is Status.SKIP
    assert r.skipped_dep == 'db'
    assert 'POSTGRES_URI' in r.detail


def test_present_dep_runs(monkeypatch):
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub')

    @check(name='needs_db_ok', tags=['unit'], requires=['db'])
    def _():
        return Status.PASS, 'ran'

    r = run_one('needs_db_ok')
    assert r.status is Status.PASS


def test_duplicate_name_raises():
    @check(name='dup', tags=['unit'], requires=['fs'])
    def _a():
        return Status.PASS, ''

    with pytest.raises(ValueError, match='duplicate'):
        @check(name='dup', tags=['unit'], requires=['fs'])
        def _b():
            return Status.PASS, ''


def test_unknown_check_raises():
    with pytest.raises(KeyError, match='unknown check'):
        run_one('does_not_exist')


def test_run_all_filters_by_tag():
    @check(name='a', tags=['x'], requires=['fs'])
    def _a():
        return Status.PASS, ''

    @check(name='b', tags=['y'], requires=['fs'])
    def _b():
        return Status.PASS, ''

    @check(name='c', tags=['x', 'y'], requires=['fs'])
    def _c():
        return Status.PASS, ''

    only_x = run_all(tags=['x'])
    names = {r.name for r in only_x}
    assert names == {'a', 'c'}


def test_run_all_filters_by_name():
    @check(name='a', tags=['unit'], requires=['fs'])
    def _a():
        return Status.PASS, ''

    @check(name='b', tags=['unit'], requires=['fs'])
    def _b():
        return Status.PASS, ''

    res = run_all(names=['b'])
    assert [r.name for r in res] == ['b']


def test_summarize_counts_each_status():
    results = [
        CheckResult(name='p1', status=Status.PASS),
        CheckResult(name='p2', status=Status.PASS),
        CheckResult(name='w1', status=Status.WARN),
        CheckResult(name='f1', status=Status.FAIL),
        CheckResult(name='s1', status=Status.SKIP),
    ]
    s = summarize(results)
    assert s['total'] == 5
    assert s['PASS'] == 2 and s['WARN'] == 1 and s['FAIL'] == 1 and s['SKIP'] == 1


def test_check_result_to_dict_serializable():
    r = CheckResult(name='x', status=Status.WARN, detail='hi', elapsed_ms=42,
                    tags=['a'], requires=['fs'])
    d = r.to_dict()
    assert d['status'] == 'WARN'   # enum coerced to string
    assert d['detail'] == 'hi'
    # Must round-trip through JSON
    assert json.loads(json.dumps(d))['status'] == 'WARN'


def test_cli_list_mode_returns_zero(capsys):
    @check(name='hello', tags=['unit'], requires=['fs'])
    def _():
        return Status.PASS, ''

    rc = cli.main(['--list', '--no-color'])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'hello' in out


def test_cli_fail_exits_2(capsys):
    @check(name='bad', tags=['unit'], requires=['fs'])
    def _():
        return Status.FAIL, 'broken'

    rc = cli.main(['--no-color'])
    assert rc == 2


def test_cli_warn_exits_1(capsys):
    @check(name='meh', tags=['unit'], requires=['fs'])
    def _():
        return Status.WARN, 'eh'

    rc = cli.main(['--no-color'])
    assert rc == 1


def test_cli_all_pass_exits_0(capsys):
    @check(name='good', tags=['unit'], requires=['fs'])
    def _():
        return Status.PASS, 'ok'

    rc = cli.main(['--no-color'])
    assert rc == 0


def test_cli_json_output_parseable(capsys):
    @check(name='ok', tags=['unit'], requires=['fs'])
    def _():
        return Status.PASS, 'fine'

    rc = cli.main(['--json', '--no-color'])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['summary']['PASS'] == 1
    assert payload['results'][0]['name'] == 'ok'
    assert payload['results'][0]['status'] == 'PASS'
