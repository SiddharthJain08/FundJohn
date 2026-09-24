"""Stream B migrations 155/156 — static DDL shape (NO database connection).

tests/database/test_sp7_migrations.py proves a migration applies by connecting
to the REAL Postgres. This box runs a live fleet backtest and the Stream B plan
forbids any test that reaches the DB, so Stream B asserts the DDL TEXT instead:
every column the ingest / ownership code writes must be declared, every statement
must be idempotent, and nothing may violate the append-only invariant.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIG = ROOT / 'src' / 'database' / 'migrations'

BROKER_FILLS_COLUMNS = (
    'activity_id', 'order_id', 'parent_order_id', 'client_order_id',
    'ticker', 'side', 'order_type', 'order_class', 'qty', 'price',
    'filled_at', 'ingested_at',
)


def _sql(name: str) -> str:
    return (MIG / name).read_text()


def _create_body(sql: str, table: str) -> str:
    start = sql.index(f'CREATE TABLE IF NOT EXISTS {table}')
    return sql[start:sql.index(');', start)]


def test_155_declares_every_broker_fills_column():
    body = _create_body(_sql('155_broker_fills.sql'), 'broker_fills')
    for col in BROKER_FILLS_COLUMNS:
        assert re.search(rf'^\s*{col}\s+\w', body, re.M), f'broker_fills.{col} not declared'


def test_155_activity_id_is_the_primary_key():
    assert re.search(r'activity_id\s+TEXT\s+PRIMARY KEY', _sql('155_broker_fills.sql'))


def test_155_indexes_the_exit_leg_join_keys():
    sql = _sql('155_broker_fills.sql')
    assert 'broker_fills_parent_idx' in sql
    assert 'broker_fills_ticker_idx' in sql


def test_155_adds_filled_at_and_exit_slippage():
    sql = _sql('155_broker_fills.sql')
    assert 'ALTER TABLE alpaca_submissions ADD COLUMN IF NOT EXISTS filled_at TIMESTAMPTZ' in sql
    assert 'ALTER TABLE signal_pnl ADD COLUMN IF NOT EXISTS exit_slippage_bps NUMERIC' in sql


def test_155_is_idempotent_and_append_only():
    sql = _sql('155_broker_fills.sql')
    assert 'CREATE TABLE IF NOT EXISTS' in sql
    upper = sql.upper()
    for stmt in ('DROP ', 'DELETE ', 'TRUNCATE '):
        assert stmt not in upper, f'{stmt.strip()} violates the append-only invariant'


def test_155_is_the_next_free_number():
    existing = sorted(p.name for p in MIG.glob('*.sql'))
    assert '155_broker_fills.sql' in existing
    assert not any(n.startswith('155_') and n != '155_broker_fills.sql' for n in existing)


POSITION_OWNERSHIP_COLUMNS = (
    'cycle_date', 'ticker', 'account_qty', 'signal_qty', 'unknown_qty',
    'status', 'created_at',
)


def test_156_declares_every_position_ownership_column():
    body = _create_body(_sql('156_position_ownership.sql'), 'position_ownership')
    for col in POSITION_OWNERSHIP_COLUMNS:
        assert re.search(rf'^\s*{col}\s+\w', body, re.M), f'position_ownership.{col} missing'


def test_156_primary_key_supports_the_upsert():
    assert 'PRIMARY KEY (cycle_date, ticker)' in _sql('156_position_ownership.sql')


def test_156_is_idempotent_and_never_deletes():
    """position_ownership is a derived ledger, not append-only: the
    reconcile step upserts it (INSERT … ON CONFLICT DO UPDATE) keyed on
    (cycle_date, ticker) — the invariant is idempotent + never DELETE, not
    "only ever inserted"."""
    upper = _sql('156_position_ownership.sql').upper()
    assert 'CREATE TABLE IF NOT EXISTS' in _sql('156_position_ownership.sql')
    for stmt in ('DROP ', 'DELETE ', 'TRUNCATE '):
        assert stmt not in upper, f'{stmt.strip()} violates the never-DELETE invariant'


def test_156_is_the_next_free_number():
    existing = sorted(p.name for p in MIG.glob('*.sql'))
    assert '156_position_ownership.sql' in existing
    assert not any(n.startswith('156_') and n != '156_position_ownership.sql'
                   for n in existing)
