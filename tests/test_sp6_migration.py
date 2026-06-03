from __future__ import annotations
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest
import psycopg2

pytestmark = pytest.mark.integration


@pytest.fixture
def db_conn():
    """psycopg2 connection to live DB, rolled back after each test."""
    uri = os.environ['POSTGRES_URI']
    conn = psycopg2.connect(uri)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


def test_migration_126_execution_signals_new_columns(db_conn):
    """Assert execution_signals table has all new SP-6 lifecycle columns."""
    cur = db_conn.cursor()
    cur.execute("""
        SELECT column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_name = 'execution_signals'
      ORDER BY ordinal_position
    """)
    cols = {name: (dtype, nullable) for name, dtype, nullable in cur.fetchall()}

    # Assert new columns exist
    expected_columns = {
        'lifecycle_state': ('text', 'YES'),
        'target_date': ('date', 'YES'),
        'computed_at': ('timestamp with time zone', 'YES'),
        'approved_at': ('timestamp with time zone', 'YES'),
        'executing_at': ('timestamp with time zone', 'YES'),
        'filled_at': ('timestamp with time zone', 'YES'),
        'gate_verdict': ('jsonb', 'YES'),
        'fill_price': ('numeric', 'YES'),
        'mark_entry_price': ('numeric', 'YES'),
    }

    for col_name, (expected_type, expected_nullable) in expected_columns.items():
        assert col_name in cols, f'missing column: {col_name}'
        actual_type, actual_nullable = cols[col_name]
        assert actual_type == expected_type, \
            f'{col_name}: expected {expected_type}, got {actual_type}'
        assert actual_nullable == expected_nullable, \
            f'{col_name}: expected nullable={expected_nullable}, got {actual_nullable}'


def test_migration_126_signal_gate_verdicts_table(db_conn):
    """Assert signal_gate_verdicts table exists with correct schema."""
    cur = db_conn.cursor()

    # Check table exists
    cur.execute("""
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'signal_gate_verdicts'
    """)
    assert cur.fetchone() is not None, 'signal_gate_verdicts table does not exist'

    # Check columns
    cur.execute("""
        SELECT column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_name = 'signal_gate_verdicts'
      ORDER BY ordinal_position
    """)
    cols = {name: (dtype, nullable) for name, dtype, nullable in cur.fetchall()}

    expected_columns = {
        'id': ('bigint', 'NO'),
        'signal_id': ('uuid', 'YES'),
        'gate_type': ('text', 'YES'),
        'ticker': ('text', 'YES'),
        'target_date': ('date', 'YES'),
        'verdict': ('text', 'YES'),
        'panic_score': ('numeric', 'YES'),
        'news_count': ('integer', 'YES'),
        'severity': ('integer', 'YES'),
        'model': ('text', 'YES'),
        'metadata': ('jsonb', 'YES'),
        'actor': ('text', 'YES'),
        'decided_at': ('timestamp with time zone', 'YES'),
    }

    for col_name, (expected_type, expected_nullable) in expected_columns.items():
        assert col_name in cols, f'signal_gate_verdicts: missing column {col_name}'
        actual_type, actual_nullable = cols[col_name]
        assert actual_type == expected_type, \
            f'signal_gate_verdicts.{col_name}: expected {expected_type}, got {actual_type}'
        assert actual_nullable == expected_nullable, \
            f'signal_gate_verdicts.{col_name}: expected nullable={expected_nullable}, got {actual_nullable}'


def test_migration_126_eod_compute_health_table(db_conn):
    """Assert eod_compute_health table exists with correct schema."""
    cur = db_conn.cursor()

    # Check table exists
    cur.execute("""
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'eod_compute_health'
    """)
    assert cur.fetchone() is not None, 'eod_compute_health table does not exist'

    # Check columns
    cur.execute("""
        SELECT column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_name = 'eod_compute_health'
      ORDER BY ordinal_position
    """)
    cols = {name: (dtype, nullable) for name, dtype, nullable in cur.fetchall()}

    expected_columns = {
        'id': ('bigint', 'NO'),
        'run_date': ('date', 'YES'),
        'run_at': ('timestamp with time zone', 'YES'),
        'rc': ('integer', 'YES'),
        'n_strategies_ok': ('integer', 'YES'),
        'n_strategies_total': ('integer', 'YES'),
        'regime_ok': ('boolean', 'YES'),
        'universe_size': ('integer', 'YES'),
        'healthy': ('boolean', 'YES'),
        'detail': ('jsonb', 'YES'),
    }

    for col_name, (expected_type, expected_nullable) in expected_columns.items():
        assert col_name in cols, f'eod_compute_health: missing column {col_name}'
        actual_type, actual_nullable = cols[col_name]
        assert actual_type == expected_type, \
            f'eod_compute_health.{col_name}: expected {expected_type}, got {actual_type}'
        assert actual_nullable == expected_nullable, \
            f'eod_compute_health.{col_name}: expected nullable={expected_nullable}, got {actual_nullable}'


def test_migration_126_signal_gate_verdicts_index(db_conn):
    """Assert signal_gate_verdicts has the required (target_date, ticker) index."""
    cur = db_conn.cursor()
    cur.execute("""
        SELECT indexname FROM pg_indexes
         WHERE tablename = 'signal_gate_verdicts'
    """)
    indexes = {name for (name,) in cur.fetchall()}

    # There should be at least one index on (target_date, ticker)
    target_index_found = any('target_date' in idx and 'ticker' in idx for idx in indexes)
    assert target_index_found, f'expected index on (target_date, ticker); got {indexes}'


def test_migration_126_eod_compute_health_index(db_conn):
    """Assert eod_compute_health has the required (run_date DESC) index."""
    cur = db_conn.cursor()
    cur.execute("""
        SELECT indexname FROM pg_indexes
         WHERE tablename = 'eod_compute_health'
    """)
    indexes = {name for (name,) in cur.fetchall()}

    # There should be at least one index on run_date
    run_date_index_found = any('run_date' in idx for idx in indexes)
    assert run_date_index_found, f'expected index on (run_date DESC); got {indexes}'


def test_migration_126_execution_signals_unique_constraint_preserved(db_conn):
    """Assert the original UNIQUE(strategy_id, signal_date, ticker, direction) is preserved."""
    cur = db_conn.cursor()
    cur.execute("""
        SELECT kcu.column_name
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
           AND tc.table_name = kcu.table_name
         WHERE tc.table_name = 'execution_signals'
           AND tc.constraint_type = 'UNIQUE'
    """)
    constraint_cols = {row[0] for row in cur.fetchall()}
    assert constraint_cols == {'strategy_id', 'signal_date', 'ticker', 'direction'}, \
        f'UNIQUE constraint columns wrong: got {constraint_cols}'
