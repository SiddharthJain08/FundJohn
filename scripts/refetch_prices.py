#!/usr/bin/env python3
"""scripts/refetch_prices.py — all-asset intraday-snapshot price refetch for
the regime prefetch (OPENCLAW_INTRADAY_15MIN_PREFETCH). Sets the prefetch
sentinel, delegates the actual fetch to the JS collector's intraday-snapshot
stage (today's partial daily bar across equity/ETF/crypto/index/forex), then
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

# Freshness cutoff: date_to >= date (TODAY).
#
# Task 3.5 (2026-06-09): the fetch now runs the all-asset INTRADAY SNAPSHOT
# stage (`--intraday-snapshot` → runIntradaySnapshotPrices), whose `dailyBar`
# carries today's partial bar mid-session.  Because that fetch writes a row for
# TODAY, `updateCoverage` advances date_to to today (rows > 0).  The old
# date−1 lag rationale — that Alpaca's daily-bar endpoint only finalized after
# close, so date_to never reached today — no longer applies on the snapshot
# path.  We therefore require date_to >= today: a fresh snapshot must show up
# as today-coverage, and a run that didn't reach today is genuinely stale.
# rc == 0 from the collector remains the primary gate; this is the secondary
# data-coverage guard.
def _freshness_cutoff(date: str) -> str:
    """Coverage cutoff for the freshness guard: require TODAY's row."""
    return date


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
    """Invoke the JS collector's all-asset intraday-snapshot stage. Returns
    its exit code.

    124 = timeout (20 min), 125 = spawn error (node not on PATH / OSError),
    any other non-zero = collector reported failure.
    """
    cmd = ['node', str(ROOT / 'src' / 'pipeline' / 'run_collector_once.js'), '--intraday-snapshot']
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
    date_to is at or past `_freshness_cutoff(date)` (TODAY).

    The intraday-snapshot fetch writes today's partial bar, so date_to should
    advance to today on a successful run (see module comment).
    """
    try:
        import psycopg2
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            return (False, 0)
        cutoff = _freshness_cutoff(date)
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


def run(date: str, episode: str | None = None) -> int:
    try:
        r = _redis()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if episode is not None:
            # Episode-bound (tick-3 gate) refetch: FORCE-overwrite any stale
            # sentinel so the gate's done/freshness check binds to THIS
            # transition's episode. set_prefetch_done/failed below preserve it.
            p.set_prefetch_running(r, date, target_state='(refetch)',
                                   episode=episode, started_at=now)
        elif p.should_prefetch(r, date, episode=f'{date}:refetch'):
            # Legacy/standalone refetch: only set running if no sentinel exists.
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
    ap.add_argument('--episode', default=None)
    args = ap.parse_args(argv)
    return run(args.date, episode=args.episode)


if __name__ == '__main__':
    raise SystemExit(main())
