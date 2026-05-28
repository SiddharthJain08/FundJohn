import os
import psycopg2
import pytest

DSN = os.environ.get('POSTGRES_URI')

@pytest.mark.skipif(DSN is None, reason='POSTGRES_URI not set')
def test_migration_120_creates_table_with_expected_columns():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'premarket_panic_alerts'
          ORDER BY ordinal_position
        """)
        cols = {name: dtype for name, dtype in cur.fetchall()}

    expected = {
        'id', 'scan_ts', 'scan_label', 'trading_day', 'ticker',
        'held_qty', 'avg_entry_price',
        'news_count_window', 'news_finbert_neg_ratio', 'news_finbert_mean_score',
        'social_post_count_window', 'social_bear_ratio',
        'panic_score', 'advisory_fired',
        'sonnet_verdict', 'sonnet_severity', 'sonnet_rationale',
        'sonnet_evidence_uuids', 'sonnet_cost_usd',
        'autoclose_fired', 'autoclose_liquidation_id',
        'realized_open_to_open_pct', 'realized_open_to_close_pct',
        'realized_backfilled_at', 'created_at',
    }
    missing = expected - cols.keys()
    assert not missing, f'missing columns: {missing}'
