"""Migration 166 — static DDL text checks only (NO database connection)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIG = ROOT / 'src' / 'database' / 'migrations'
F166 = '166_backtest_trades_primary.sql'


def _sql(name: str) -> str:
    return (MIG / name).read_text()


def _create_body(sql: str, table: str) -> str:
    start = sql.index(f'CREATE TABLE IF NOT EXISTS {table}')
    return sql[start:sql.index(');', start)]


def _base_columns():
    sql = _sql('093_unified_backtest_tables.sql')
    start = sql.index('CREATE TABLE IF NOT EXISTS strategy_backtest_trades (')
    body = sql[start:sql.index('PRIMARY KEY', start)]
    cols = []
    for line in body.splitlines()[1:]:
        line = line.split('--')[0].strip().rstrip(',').strip()
        if not line:
            continue
        name, decl = line.split(None, 1)
        decl = re.sub(r'\s+REFERENCES\s.*$', '', decl)
        cols.append((name, decl))
    return cols


def test_166_declares_every_base_column_with_same_type():
    cols = _base_columns()
    assert len(cols) == 15
    hot = _create_body(_sql(F166), 'strategy_backtest_trades_primary')
    for name, decl in cols:
        assert re.search(rf'^\s*{name}\s+{re.escape(decl)}\s*,', hot, re.M), name


def test_166_primary_key_run_id_trade_seq():
    assert re.search(r'PRIMARY KEY\s*\(\s*run_id\s*,\s*trade_seq\s*\)', _sql(F166))


def test_166_every_create_is_idempotent():
    assert not re.search(r'CREATE (TABLE|INDEX)(?! IF NOT EXISTS)', _sql(F166))


def test_166_backfill_is_guarded_locked_and_marked():
    sql = _sql(F166)
    assert "'backfilled:166'" in sql and 'obj_description' in sql
    assert 'LOCK TABLE strategy_backtest_runs IN SHARE MODE' in sql
    assert 'SET LOCAL statement_timeout = 0;' in sql
    assert "SET LOCAL lock_timeout = '60s';" in sql
    assert 'set_config' not in sql
    assert sql.index('SET LOCAL statement_timeout') < sql.index('DO $$')
    assert sql.index("SET LOCAL lock_timeout") < sql.index('DO $$')
    assert 'ON CONFLICT (run_id, trade_seq) DO NOTHING' in sql


def test_166_secondary_index_built_after_backfill():
    sql = _sql(F166)
    assert sql.index('idx_bt_trades_primary_strategy') > sql.index('END LOOP')


def test_166_never_mutates_the_base_table():
    sql = _sql(F166)
    assert not re.search(r'(DELETE FROM|UPDATE|TRUNCATE)\s+strategy_backtest_trades\b(?!_primary)', sql)
    assert not re.search(r'^\s*COMMIT\b', sql, re.M | re.I)
