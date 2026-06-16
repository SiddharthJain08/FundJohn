import os, psycopg2, pytest
PG = os.environ.get('POSTGRES_URI')

@pytest.mark.skipif(not PG, reason='no POSTGRES_URI')
def test_origin_column_default_paper():
    conn = psycopg2.connect(PG); cur = conn.cursor()
    cur.execute("""SELECT column_name, column_default FROM information_schema.columns
                   WHERE table_name='research_candidates' AND column_name IN ('origin','reference_url')
                   ORDER BY column_name""")
    cols = dict((r[0], r[1]) for r in cur.fetchall())
    conn.close()
    assert 'origin' in cols and 'reference_url' in cols
    assert "'paper'" in (cols['origin'] or '')
