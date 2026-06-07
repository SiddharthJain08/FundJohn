"""SP-7 Phase B Task 2 — migrations 131/132 round-trip (rollback)."""
from __future__ import annotations
import os
import sys
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[1]
MIG = ROOT / 'src' / 'database' / 'migrations'

URI = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL')
pytestmark = pytest.mark.skipif(not URI, reason='POSTGRES_URI not set')


@pytest.mark.parametrize('fname,table,expected_not_null,expected_indices', [
    (
        '131_universe_ladder_runs.sql',
        'universe_ladder_runs',
        {'run_id', 'strategy_id', 'tier', 'status', 'queued_at'},
        {'idx_ulr_run_status', 'idx_ulr_strategy', 'universe_ladder_runs_pkey', 'universe_ladder_runs_run_id_strategy_id_tier_key'},
    ),
    (
        '132_universe_threshold_proposals.sql',
        'universe_threshold_proposals',
        {'proposer', 'regime_state', 'proposed_min_cumulative_sharpe', 'status'},
        {'idx_utp_status', 'universe_threshold_proposals_pkey'},
    ),
])
def test_migration_round_trip(fname, table, expected_not_null, expected_indices):
    sql = (MIG / fname).read_text()
    conn = psycopg2.connect(URI)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] >= 0

            # Assert NOT NULL columns exist and have correct nullability.
            cur.execute(
                """
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_name = %s
                """,
                (table,),
            )
            got_cols = {r[0]: r[1] for r in cur.fetchall()}
            for col in expected_not_null:
                assert col in got_cols, f"missing NOT NULL column {col} in {table}"
                assert got_cols[col] == 'NO', f"{table}.{col}: nullability {got_cols[col]} != expected NO"

            # Assert expected indices exist.
            cur.execute(
                """
                SELECT indexname FROM pg_indexes
                WHERE tablename = %s ORDER BY 1
                """,
                (table,),
            )
            got_indices = {r[0] for r in cur.fetchall()}
            for idx in expected_indices:
                assert idx in got_indices, f"missing index {idx} in {table}"

            cur.execute(sql)  # idempotency: re-apply inside same txn must not raise
    finally:
        # Rollback (not commit + cleanup): tables must NOT land in the live DB
        # before the runbook applies migrations.
        conn.rollback()
        conn.close()
