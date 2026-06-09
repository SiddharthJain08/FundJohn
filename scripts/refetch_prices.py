#!/usr/bin/env python3
"""scripts/refetch_prices.py — prices-only intraday refetch for the regime
prefetch (OPENCLAW_INTRADAY_15MIN_PREFETCH). Sets the prefetch sentinel,
delegates the actual fetch to the JS collector's price-fill stage, then
verifies freshness. Never deletes master rows (collector is append-dedup).

Exit 0 = fresh prices written + sentinel 'done'. Exit 1 = sentinel 'failed'.
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.execution import intraday_prefetch as p   # noqa: E402

FRESHNESS_TABLE_DAYS = 1   # union prices must cover within today


def _redis():
    try:
        import redis
        url = os.environ.get('REDIS_URL')
        if not url:
            return None
        return redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
    except Exception:
        return None


def _run_price_fill(date: str) -> int:
    """Invoke the JS collector's prices-only stage. Returns its exit code."""
    cmd = ['node', str(ROOT / 'src' / 'pipeline' / 'run_collector_once.js'), '--prices-only']
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), timeout=20 * 60,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            print(proc.stdout.decode()[-2000:])
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124


def _freshness_ok(date: str) -> tuple[bool, int]:
    """True + covered-ticker-count if data_coverage shows union prices updated
    to within FRESHNESS_TABLE_DAYS of `date`."""
    try:
        import psycopg2
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            return (False, 0)
        cutoff = (dt.date.fromisoformat(date) - dt.timedelta(days=FRESHNESS_TABLE_DAYS)).isoformat()
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM data_coverage WHERE data_type='prices' AND date_to >= %s",
            (cutoff,))
        n = int(cur.fetchone()[0])
        cur.close(); conn.close()
        return (n > 0, n)
    except Exception as e:
        print(f'[refetch] freshness check error: {e}')
        return (False, 0)


def run(date: str) -> int:
    r = _redis()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if p.read_prefetch(r, date) is None:
        p.set_prefetch_running(r, date, target_state='(refetch)',
                               episode=f'{date}:refetch', started_at=now)
    rc = _run_price_fill(date)
    fresh, n = _freshness_ok(date)
    fin = dt.datetime.now(dt.timezone.utc).isoformat()
    if rc == 0 and fresh:
        p.set_prefetch_done(r, date, n_tickers=n, finished_at=fin)
        print(f'[refetch] done n_tickers={n}')
        return 0
    p.set_prefetch_failed(r, date, error=f'rc={rc} fresh={fresh}', finished_at=fin)
    print(f'[refetch] FAILED rc={rc} fresh={fresh}')
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=dt.date.today().isoformat())
    args = ap.parse_args(argv)
    return run(args.date)


if __name__ == '__main__':
    raise SystemExit(main())
