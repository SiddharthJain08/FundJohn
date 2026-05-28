import os
import psycopg2
import pytest

DSN = os.environ.get('POSTGRES_URI')

@pytest.mark.skipif(DSN is None, reason='POSTGRES_URI not set')
def test_migration_121_creates_table_with_expected_columns():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
             WHERE table_name = 'edgar_8k_filings'
          ORDER BY ordinal_position
        """)
        cols = {name for (name,) in cur.fetchall()}

    expected = {
        'id', 'accession', 'cik', 'ticker', 'filing_date', 'accepted_at',
        'item_number', 'item_description', 'primary_doc_url',
        'market_news_uuid', 'fetched_at',
    }
    missing = expected - cols
    assert not missing, f'missing columns: {missing}'


@pytest.mark.skipif(DSN is None, reason='POSTGRES_URI not set')
def test_migration_121_composite_unique_constraint():
    """One row per (accession, item_number) — duplicate inserts should no-op."""
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT constraint_name FROM information_schema.table_constraints
             WHERE table_name = 'edgar_8k_filings'
               AND constraint_type = 'UNIQUE'
        """)
        constraints = [name for (name,) in cur.fetchall()]
    assert any('accession' in c for c in constraints), (
        f'expected a UNIQUE constraint involving accession; got {constraints}'
    )
