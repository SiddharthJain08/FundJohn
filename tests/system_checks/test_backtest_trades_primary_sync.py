import sys

import psycopg2

from src.backtest import trades_primary as tp
from src.system_checks.checks import backtest_trades_primary as chk
from src.system_checks.types import Status


def test_pass_when_in_sync():
    st, d = chk.evaluate(table_exists=True, count_mismatches=[], orphan_runs=0, id_mismatches=0, n_primary_runs=304)
    assert st is Status.PASS and '304 primary runs in sync' in d


def test_warn_when_table_absent():
    st, d = chk.evaluate(table_exists=False, count_mismatches=[], orphan_runs=0, id_mismatches=0, n_primary_runs=0)
    assert st is Status.WARN and 'migration 166' in d


def test_count_mismatch_fails_and_names_strategy():
    st, d = chk.evaluate(table_exists=True, count_mismatches=[('r1', 'S_ZZTEST1', 120, 0)],
                         orphan_runs=0, id_mismatches=0, n_primary_runs=304)
    assert st is Status.FAIL and 'S_ZZTEST1' in d and '--resync-drifted' in d and len(d) < 200


def test_orphan_rows_fail():
    assert chk.evaluate(table_exists=True, count_mismatches=[], orphan_runs=2, id_mismatches=0, n_primary_runs=304)[0] is Status.FAIL


def test_strategy_id_mismatch_fails():
    assert chk.evaluate(table_exists=True, count_mismatches=[], orphan_runs=0, id_mismatches=1, n_primary_runs=304)[0] is Status.FAIL


def test_check_is_registered():
    import src.system_checks.checks  # noqa: F401
    from src.system_checks.registry import all_checks
    assert 'backtest_trades_primary_sync' in all_checks()


class FakeCur:
    def __init__(self, table=True):
        self.table, self.sql, self.rowcount = table, [], 0
    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self.last = sql
    def fetchone(self):
        return (self.table,)
    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, table=True):
        self.cur, self.commits, self.autocommit = FakeCur(table), 0, False
    def cursor(self):
        return self.cur
    def commit(self):
        self.commits += 1
    def rollback(self):
        pass
    def close(self):
        pass


def test_check_skips_cleanly_when_table_missing(monkeypatch):
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://x')
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: FakeConn(table=False))
    st, d = chk._backtest_trades_primary_sync()
    assert st is Status.WARN and 'migration 166' in d


def test_cli_resync_commits_per_strategy(monkeypatch, capsys):
    conn = FakeConn()
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://x')
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: conn)
    calls = []
    monkeypatch.setattr(tp, '_drifted_strategy_ids', lambda cur: ['S_A', 'S_B'])
    monkeypatch.setattr(tp, 'resync_strategy', lambda cur, sid: calls.append(sid) or 7)
    assert tp.main(['--resync-drifted']) == 0
    assert calls == ['S_A', 'S_B']
    assert conn.commits == 3  # 1 closing the read txn + 1 per strategy
    assert '[bt_trades_primary] resync strategy=S_A inserted=7' in capsys.readouterr().out
