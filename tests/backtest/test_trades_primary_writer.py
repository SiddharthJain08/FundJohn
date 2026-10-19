"""Task 2 (Stream E): hot-table writer maintenance. No DB: cursor is faked."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backtest import trades_primary as tp
from backtest import unified_backtest as ub

ROW = ('r', 1, 'S_ZZTEST1', 'AAA', 'long', '2024-01-02', 1.0, '2024-01-05', 1.1,
       'target', 0.1, 3, 'LOW_VOL', 0.9, 1.2)
ROW2 = ROW[:1] + (2,) + ROW[2:]


class FakeCursor:
    def __init__(self, table_exists=True, delete_rowcount=0, insert_rowcount=0):
        self.table_exists = table_exists
        self.delete_rowcount = delete_rowcount
        self.insert_rowcount = insert_rowcount
        self.calls = []
        self.rowcount = -1
        self._row = None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        s = sql.strip()
        if 'to_regclass' in s:
            self._row = (self.table_exists,)
        elif s.startswith('DELETE'):
            self.rowcount = self.delete_rowcount
        elif s.startswith('INSERT'):
            self.rowcount = self.insert_rowcount

    def fetchone(self):
        return self._row


class _EV:
    def __init__(self):
        self.calls = 0
        self.sql = None
        self.rows = None
        self.page_size = None


def record_execute_values(monkeypatch):
    import psycopg2.extras
    ev = _EV()

    def fake(cur, sql, rows, page_size=100, **kw):
        ev.calls += 1
        ev.sql, ev.rows, ev.page_size = sql, rows, page_size
        cur.calls.append((sql, None))
    monkeypatch.setattr(psycopg2.extras, 'execute_values', fake)
    return ev


def test_sync_absent_table_skips_and_logs(capsys):
    cur = FakeCursor(table_exists=False)
    assert tp.sync_primary_trades(cur, 'S_ZZTEST1', 'r1', [ROW]) is None
    assert not any('DELETE' in s for s, _ in cur.calls)
    assert '[bt_trades_primary] table absent' in capsys.readouterr().out


def test_sync_deletes_then_inserts_in_order(capsys, monkeypatch):
    cur = FakeCursor(table_exists=True, delete_rowcount=7)
    ev = record_execute_values(monkeypatch)
    assert tp.sync_primary_trades(cur, 'S_ZZTEST1', 'r2', [ROW, ROW2]) == 2
    deletes = [c for c in cur.calls if c[0].startswith('DELETE')]
    assert deletes == [('DELETE FROM strategy_backtest_trades_primary WHERE strategy_id = %s',
                        ('S_ZZTEST1',))]
    assert cur.calls.index(deletes[0]) < len(cur.calls) - 1  # insert came after
    assert ev.sql.strip().startswith('INSERT INTO strategy_backtest_trades_primary')
    assert ev.rows == [ROW, ROW2] and ev.page_size == 500
    assert 'deleted=7 inserted=2' in capsys.readouterr().out


def test_sync_zero_trades_still_deletes(monkeypatch):
    cur = FakeCursor(table_exists=True, delete_rowcount=40)
    ev = record_execute_values(monkeypatch)
    assert tp.sync_primary_trades(cur, 'S_ZZTEST1', 'r3', []) == 0
    assert any(s.startswith('DELETE FROM strategy_backtest_trades_primary') for s, _ in cur.calls)
    assert ev.calls == 0


def test_resync_locks_deletes_then_inserts_from_primary_runs():
    cur = FakeCursor(table_exists=True, insert_rowcount=12)
    assert tp.resync_strategy(cur, 'S_ZZTEST1') == 12
    sqls = [s.strip() for s, _ in cur.calls]
    assert sqls[0] == 'LOCK TABLE strategy_backtest_runs IN SHARE MODE'
    assert sqls[1].startswith('DELETE FROM strategy_backtest_trades_primary')
    assert sqls[2].startswith('INSERT INTO strategy_backtest_trades_primary')
    assert 'r.primary_window' in sqls[2] and cur.calls[2][1] == ('S_ZZTEST1',)
    assert not any('DELETE FROM strategy_backtest_trades ' in s for s in sqls)


def test_trade_columns_match_unified_backtest_insert():
    src = Path(ub.__file__).read_text()
    block = src[src.index('INSERT INTO strategy_backtest_trades\n'):]
    assert tuple(re.findall(r'\w+', block[block.index('(') + 1:block.index(')')])) == tp.TRADE_COLUMNS


def test_persist_syncs_after_demotion_before_commit():
    src = Path(ub.__file__).read_text()
    i_dem = src.index('SET primary_window = FALSE')
    i_sync = src.index('sync_primary_trades(cur, strategy_id, run_id, trade_rows)')
    i_commit = src.index('conn.commit()', i_dem)
    assert i_dem < i_sync < i_commit
    assert 'trade_rows = []' in src[src.index('regime_rows = []'):i_dem]
