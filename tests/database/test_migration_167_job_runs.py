"""Static DDL shape for migration 167."""
import re
from pathlib import Path

SQL = (Path(__file__).resolve().parents[2] / 'src/database/migrations/167_scheduled_job_runs.sql').read_text()


def test_167_columns_and_source_check():
    flat = re.sub(r'\s+', ' ', SQL)
    for col in ('id BIGSERIAL PRIMARY KEY', 'job_name TEXT NOT NULL',
                "CHECK (source IN ('systemd','cron'))",
                'started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()', 'finished_at   TIMESTAMPTZ',
                'success       BOOLEAN', 'rc            INT', 'host          TEXT',
                'pid           INT', 'invocation_id TEXT', 'note          TEXT'):
        assert re.sub(r'\s+', ' ', col) in flat, col


def test_167_idempotent_and_indexed():
    for m in re.finditer(r'CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+(?!IF NOT EXISTS)', SQL):
        raise AssertionError(m.group(0))
    assert 'idx_scheduled_job_runs_job' in SQL and 'idx_scheduled_job_runs_open' in SQL
    assert 'WHERE finished_at IS NULL' in SQL
