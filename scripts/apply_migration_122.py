"""Apply migration 122 idempotently via psycopg2. Mirrors mig 119 pattern."""
import os, sys, psycopg2


def _uri():
    with open('.env') as f:
        for line in f:
            if line.startswith('POSTGRES_URI='):
                return line.split('=', 1)[1].strip().strip('"\'')
    raise RuntimeError('POSTGRES_URI missing from .env')


def main():
    sql = open('src/database/migrations/122_alpaca_submissions_instrument_class.sql').read()
    conn = psycopg2.connect(_uri())
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='alpaca_submissions' AND column_name='instrument_class'"
    )
    assert cur.fetchone(), "post-condition: instrument_class column not present"
    cur.close()
    conn.close()
    print("migration 122 applied and verified")


if __name__ == '__main__':
    sys.exit(main())
