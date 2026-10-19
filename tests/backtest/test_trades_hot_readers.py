"""Task 3 (E1): the six backtest-trades readers swap ONLY the table name,
behind OPENCLAW_BT_TRADES_HOT=1. Connections are stubbed; SQL text is asserted."""
import datetime as dt
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import trades_primary as tp  # noqa: E402
import backtest.backtest_panel as bp  # noqa: E402
import execution.backtest_coupled_recs as bcr  # noqa: E402
import execution.strategy_similarity as ss  # noqa: E402

LEGACY, HOT = 'strategy_backtest_trades ', 'strategy_backtest_trades_primary '

LEGACY_RETURNS_SQL = """
        SELECT t.entry_regime, t.strategy_id, t.exit_date::text, AVG(t.pnl_pct)::float
          FROM strategy_backtest_trades t
          JOIN strategy_backtest_runs r ON r.run_id = t.run_id AND r.primary_window
         WHERE t.exit_date IS NOT NULL AND t.entry_regime IS NOT NULL
         GROUP BY 1, 2, 3
    """


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.sqls.append(sql)

    def fetchall(self):
        return []

    def fetchone(self):
        return self.conn.fetchones.pop(0) if self.conn.fetchones else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, fetchones=()):
        self.sqls = []
        self.fetchones = list(fetchones)

    def cursor(self, *a, **k):
        return FakeCursor(self)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _set(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv('OPENCLAW_BT_TRADES_HOT', raising=False)
    else:
        monkeypatch.setenv('OPENCLAW_BT_TRADES_HOT', flag)


def _trades_sql(conn):
    return next(s for s in conn.sqls if 'strategy_backtest_trades' in s)


def _via_ss(monkeypatch, flag, fn):
    _set(monkeypatch, flag)
    conn = FakeConn()
    monkeypatch.setattr(ss, '_db', lambda: conn)
    if fn is ss._cross_regime_exit_share:
        import pandas as pd
        monkeypatch.setattr(pd, 'read_parquet',
                            lambda *a, **k: pd.DataFrame({'date': [], 'regime': []}))
    fn()
    return _trades_sql(conn)


PARAMS = [(None, LEGACY), ('1', HOT)]


@pytest.mark.parametrize('flag,table', PARAMS)
@pytest.mark.parametrize('name', ['_returns_by_regime_backtest',
                                  '_cofiring_sets_by_regime_backtest',
                                  '_cross_regime_exit_share'])
def test_similarity_readers(monkeypatch, flag, table, name):
    sql = _via_ss(monkeypatch, flag, getattr(ss, name))
    if name == '_cross_regime_exit_share':
        assert f'FROM {table}t' in sql and 'r.primary_window' in sql
    else:
        assert f'FROM {table}t' in sql
        assert 'JOIN strategy_backtest_runs r ON r.run_id = t.run_id AND r.primary_window' in sql


def test_legacy_sql_is_byte_identical_when_flag_unset(monkeypatch):
    assert _via_ss(monkeypatch, None, ss._returns_by_regime_backtest) == LEGACY_RETURNS_SQL


@pytest.mark.parametrize('flag,table', PARAMS)
def test_panel_trade_tickers(monkeypatch, flag, table):
    _set(monkeypatch, flag)
    conn = FakeConn()
    bp._trade_tickers(conn, 'S1')
    sql = conn.sqls[0]
    assert f'FROM {table.strip()}\n' in sql
    assert 'WHERE strategy_id=%s AND primary_window=TRUE' in sql


@pytest.mark.parametrize('flag,table', PARAMS)
def test_panel_build_panel(monkeypatch, flag, table):
    _set(monkeypatch, flag)
    conn = FakeConn([{'run_id': 'r1', 'total_sharpe': 1.0, 'avg_holding_days': 5}])
    bp.build_panel('S1', conn, None, None, None)
    assert f'FROM {table}WHERE run_id=%s' in _trades_sql(conn)


@pytest.mark.parametrize('flag,table', PARAMS)
def test_coupled_recs_stored_baseline(monkeypatch, flag, table):
    _set(monkeypatch, flag)
    monkeypatch.setenv('POSTGRES_URI', 'postgres://stub')
    conn = FakeConn([('r1', 1.0, dt.date(2026, 1, 2), '{}'), (0.02, 0.04)])
    import psycopg2
    monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: conn)
    bcr._stored_baseline('S1')
    assert f'FROM {table.strip()}\n' in _trades_sql(conn)


def test_trades_source_reads_env_each_call(monkeypatch):
    _set(monkeypatch, None)
    assert tp.trades_source() == 'strategy_backtest_trades'
    _set(monkeypatch, '1')
    assert tp.trades_source() == tp.HOT_TABLE
    for v in ('0', 'true', ''):
        _set(monkeypatch, v)
        assert tp.trades_source() == 'strategy_backtest_trades'


def test_universe_shrink_reads_via_trades_source():
    src = (ROOT / 'scripts/run_universe_shrink.py').read_text()
    assert 'FROM {trades_source()} WHERE run_id=%s' in src
    assert 'FROM strategy_backtest_trades WHERE' not in src


def test_reader_imports_follow_path_bootstrap():
    for rel, anchor in (('src/execution/strategy_similarity.py', 'from execution.orthogonalization import'),
                        ('src/backtest/backtest_panel.py', "sys.path.insert(0, str(ROOT / 'src'))"),
                        ('scripts/run_universe_shrink.py', "sys.path.insert(0, str(ROOT / 'src'))")):
        src = (ROOT / rel).read_text()
        assert src.index(anchor) < src.index('from backtest.trades_primary import trades_source'), rel
    assert 'from backtest.trades_primary import trades_source' in inspect.getsource(bcr._stored_baseline)
    assert 'from backtest.trades_primary' not in Path(bcr.__file__).read_text().split('def _stored_baseline')[0]


def test_server_bw_query_uses_bt_trades_table():
    src = (ROOT / 'src/channels/api/server.js').read_text()
    blk = src[src.index('Best/worst realized trade %'):src.index('const bwById')]
    assert 'FROM ${btTradesTable()} t' in blk and 'DISTINCT ON (strategy_id) strategy_id, run_id' in blk
