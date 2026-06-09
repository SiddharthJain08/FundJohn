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

# Freshness cutoff: date_to >= date − 1 day.
#
# Intraday investigation (2026-06-09): the collector calls `--end today` but
# Alpaca only delivers a complete daily bar after market close, so
# `updateCoverage` sees 0 rows written for today's bar and — by design —
# refuses to advance date_to (store.js line 310: "Only advance coverage when
# we actually wrote rows").  Empirically, max(date_to) == yesterday even after
# a successful mid-session run (5027 tickers, none with date_to >= today).
# `last_updated` also does not advance on zero-row fetches, so a recency check
# would always fail intraday.
#
# Consequence: we cannot distinguish "the refetch ran and correctly found
# nothing new" from "the data is genuinely stale" using date_to or
# last_updated alone.  The correct primary gate is `rc == 0` from the
# collector.  We keep `date_to >= date − 1` as a secondary guard — it catches
# multi-day gaps or a totally blank coverage table — and lean on rc==0 as the
# real signal.  If/when Alpaca begins delivering same-session bars intraday,
# change cutoff to `date` and remove this comment.
FRESHNESS_DAYS_LAG = 1


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
    """Invoke the JS collector's prices-only stage. Returns its exit code.

    124 = timeout (20 min), 125 = spawn error (node not on PATH / OSError),
    any other non-zero = collector reported failure.
    """
    cmd = ['node', str(ROOT / 'src' / 'pipeline' / 'run_collector_once.js'), '--prices-only']
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), timeout=20 * 60,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            print(proc.stdout.decode('utf-8', errors='replace')[-2000:])
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124
    except Exception as exc:
        print(f'[refetch] spawn error: {exc}')
        return 125


def _freshness_ok(date: str) -> tuple[bool, int]:
    """True + covered-ticker-count if data_coverage shows union prices whose
    date_to is within FRESHNESS_DAYS_LAG of `date`.

    The cutoff is date − FRESHNESS_DAYS_LAG days (currently yesterday) because
    intraday fetches do not advance date_to to today (see module comment).
    """
    try:
        import psycopg2
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            return (False, 0)
        cutoff = (dt.date.fromisoformat(date) - dt.timedelta(days=FRESHNESS_DAYS_LAG)).isoformat()
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
    try:
        r = _redis()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if p.should_prefetch(r, date, episode=f'{date}:refetch'):
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
    except Exception as exc:
        print(f'[refetch] unhandled exception: {exc}')
        try:
            r_err = _redis()
            p.set_prefetch_failed(r_err, date, error=f'exception: {exc}')
        except Exception:
            pass
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=dt.date.today().isoformat())
    args = ap.parse_args(argv)
    return run(args.date)


if __name__ == '__main__':
    raise SystemExit(main())
