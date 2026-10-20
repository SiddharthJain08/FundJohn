"""tests/lib/test_job_stat.py — DB stubbed; never touches a real database."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from lib import job_stat as js  # noqa: E402


class FakeCur:
    def __init__(self, db):
        self.db = db
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.calls.append((sql, params))
        if sql.startswith('UPDATE'):
            self._rows = self.db.update_returns
        elif sql.startswith('INSERT'):
            if 'finished_at' in sql:
                self.db.last_insert_note = params[-1]
            self._rows = [(1,)]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, db):
        self.db = db
        self.autocommit = False

    def cursor(self):
        return FakeCur(self.db)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.calls = []
        self.update_returns = [(1,)]
        self.last_insert_note = None


@pytest.fixture
def fake_db(monkeypatch):
    monkeypatch.delenv('OPENCLAW_JOB_STAT', raising=False)
    db = FakeDB()
    monkeypatch.setattr(js, '_connect', lambda: FakeConn(db))
    return db


def test_systemd_outcome_success():
    assert js.systemd_outcome({'SERVICE_RESULT': 'success', 'EXIT_CODE': 'exited', 'EXIT_STATUS': '0'}) == (0, True, 'result=success exit_code=exited exit_status=0')


def test_systemd_outcome_exit_code_nonzero():
    rc, ok, _ = js.systemd_outcome({'SERVICE_RESULT': 'exit-code', 'EXIT_CODE': 'exited', 'EXIT_STATUS': '2'})
    assert (rc, ok) == (2, False)


def test_systemd_outcome_oom_kill():
    rc, ok, note = js.systemd_outcome({'SERVICE_RESULT': 'oom-kill', 'EXIT_CODE': 'killed', 'EXIT_STATUS': 'KILL'})
    assert rc is None and ok is False and 'result=oom-kill' in note


def test_finish_without_start_inserts_complete_row(fake_db):
    fake_db.update_returns = []
    assert js.record_finish('openclaw-zztest.service', 1, source='systemd', invocation_id='abc') is True
    assert fake_db.last_insert_note.startswith('no-start-row; ')


def test_finish_pairs_with_open_row(fake_db):
    assert js.record_finish('j', 0, source='systemd', invocation_id='abc') is True
    assert fake_db.last_insert_note is None


def test_db_failure_never_raises(monkeypatch, capsys):
    monkeypatch.delenv('OPENCLAW_JOB_STAT', raising=False)
    monkeypatch.setattr(js, '_connect', lambda: (_ for _ in ()).throw(OSError('down')))
    assert js.record_start('zz-job') is None
    assert '[job_stat] zz-job start write failed: OSError' in capsys.readouterr().out


def test_kill_switch(monkeypatch, fake_db):
    monkeypatch.setenv('OPENCLAW_JOB_STAT', '0')
    assert js.record_start('zz-job') is None and fake_db.calls == []


def test_main_always_returns_zero(monkeypatch):
    monkeypatch.delenv('OPENCLAW_JOB_STAT', raising=False)
    monkeypatch.setattr(js, '_connect', lambda: (_ for _ in ()).throw(OSError('down')))
    assert js.main(['finish', 'openclaw-zztest.service']) == 0 and js.main(['bogus']) == 0
