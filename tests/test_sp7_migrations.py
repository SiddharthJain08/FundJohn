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


@pytest.mark.parametrize('fname,table', [
    ('131_universe_ladder_runs.sql', 'universe_ladder_runs'),
    ('132_universe_threshold_proposals.sql', 'universe_threshold_proposals'),
])
def test_migration_round_trip(fname, table):
    sql = (MIG / fname).read_text()
    conn = psycopg2.connect(URI)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] >= 0
            cur.execute(sql)  # idempotency: re-apply inside same txn must not raise
    finally:
        conn.rollback()
        conn.close()
