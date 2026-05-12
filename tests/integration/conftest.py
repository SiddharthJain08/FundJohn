import os
import pytest
import psycopg2


@pytest.fixture
def db_conn():
    """Real Postgres connection rolled back after each test."""
    uri = os.environ.get(
        'TEST_POSTGRES_URI',
        os.environ.get('POSTGRES_URI', 'postgresql://openclaw:password@localhost:5432/openclaw'),
    )
    conn = psycopg2.connect(uri)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()
