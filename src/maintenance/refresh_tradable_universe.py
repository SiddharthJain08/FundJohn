#!/usr/bin/env python3
"""refresh_tradable_universe.py

Pulls Alpaca's master `us_equity` asset list and upserts it into
`alpaca_tradable_universe` so the trade pipeline can filter out
delisted / halted / non-tradable tickers BEFORE submission.

Designed to run on a daily timer (13:30 UTC, ~30 min before the 14:00
UTC pipeline cycle) and also as a BotJohn maintenance step.

Behaviour:
  - Fetches every active us_equity asset (~13.6k symbols at time of
    writing) via `alpaca asset list --asset-class us_equity --status active`.
  - Upserts each row, advancing last_seen_at and refreshing the
    tradable/shortable/marginable flags.
  - Any symbol previously stored but absent from today's pull is marked
    status='inactive', tradable=false (NEVER deleted — master-data rule).
  - Writes a row to `alpaca_tradable_universe_refresh_log` with diff counts.
  - Prints a one-line summary + non-zero exit code on failure so a
    systemd timer / pipeline ExecStartPre will surface the failure.

Usage:
  python3 -m maintenance.refresh_tradable_universe [--dry-run] [--verbose]

Exit codes:
  0  refresh succeeded
  1  Alpaca CLI failed / DB write failed
  2  CLI returned 0 assets (suspicious; aborts without writing)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

ALPACA_BIN = os.environ.get('ALPACA_BIN', '/root/go/bin/alpaca')
SUSPICIOUS_FLOOR = int(os.environ.get('UNIVERSE_MIN_ROWS', '5000'))


def _log(msg: str) -> None:
    print(f'[refresh_tradable_universe] {msg}', flush=True)


def fetch_alpaca_assets(timeout: int = 60) -> tuple[list[dict], int]:
    """Return (assets, elapsed_ms). Raises subprocess.CalledProcessError
    on CLI failure, ValueError if stdout isn't valid JSON list."""
    started = time.monotonic()
    proc = subprocess.run(
        [ALPACA_BIN, 'asset', 'list',
         '--asset-class', 'us_equity', '--status', 'active'],
        capture_output=True, text=True, timeout=timeout,
        env=os.environ.copy(),
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args,
            output=proc.stdout, stderr=proc.stderr,
        )
    data = json.loads(proc.stdout)
    if not isinstance(data, list):
        # CLI sometimes returns an error dict — surface it.
        if isinstance(data, dict) and 'error' in data:
            raise RuntimeError(f"alpaca CLI error: {data['error']}")
        raise ValueError(f'unexpected payload type: {type(data).__name__}')
    return data, elapsed_ms


def upsert_universe(conn, assets: Iterable[dict], dry_run: bool = False) -> dict:
    """Upsert today's pull. Returns diff stats dict.

    Stats:
      total_active    — rows in today's pull
      total_tradable  — rows with tradable=true
      newly_listed    — symbols inserted for the first time
      newly_inactive  — symbols previously seen but absent today
      status_changes  — symbols whose tradable/status flipped vs prior
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Snapshot of prior state (before this run) to compute diffs.
    cur.execute("SELECT symbol, tradable, status FROM alpaca_tradable_universe")
    prior = {r['symbol']: (r['tradable'], r['status']) for r in cur}

    today_symbols: set[str] = set()
    rows = []
    for a in assets:
        sym = a.get('symbol')
        if not sym:
            continue
        today_symbols.add(sym)
        rows.append((
            sym,
            a.get('id'),
            a.get('name'),
            a.get('class'),
            a.get('exchange'),
            'active',
            bool(a.get('tradable', False)),
            bool(a.get('shortable', False)),
            bool(a.get('marginable', False)),
            bool(a.get('fractionable', False)),
            bool(a.get('easy_to_borrow', False)),
        ))

    total_active   = len(today_symbols)
    total_tradable = sum(1 for a in assets if a.get('tradable'))

    # Diff stats
    newly_listed   = sum(1 for s in today_symbols if s not in prior)
    today_state    = {a.get('symbol'): (bool(a.get('tradable', False)), 'active') for a in assets}
    status_changes = sum(
        1 for s in today_symbols
        if s in prior and prior[s] != today_state[s]
    )
    # Symbols previously seen with status='active' or tradable=true but
    # absent from today's pull → newly inactive.
    newly_inactive = sum(
        1 for s, (tr, st) in prior.items()
        if s not in today_symbols and (st == 'active' or tr)
    )

    if dry_run:
        return {
            'total_active':   total_active,
            'total_tradable': total_tradable,
            'newly_listed':   newly_listed,
            'newly_inactive': newly_inactive,
            'status_changes': status_changes,
            'dry_run':        True,
        }

    # Bulk upsert today's pull.
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO alpaca_tradable_universe
          (symbol, alpaca_asset_id, name, asset_class, exchange,
           status, tradable, shortable, marginable, fractionable, easy_to_borrow,
           last_seen_at, updated_at)
        VALUES %s
        ON CONFLICT (symbol) DO UPDATE SET
          alpaca_asset_id = EXCLUDED.alpaca_asset_id,
          name            = EXCLUDED.name,
          asset_class     = EXCLUDED.asset_class,
          exchange        = EXCLUDED.exchange,
          status          = EXCLUDED.status,
          tradable        = EXCLUDED.tradable,
          shortable       = EXCLUDED.shortable,
          marginable      = EXCLUDED.marginable,
          fractionable    = EXCLUDED.fractionable,
          easy_to_borrow  = EXCLUDED.easy_to_borrow,
          last_seen_at    = NOW(),
          updated_at      = NOW()
        """,
        rows,
        template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())",
        page_size=1000,
    )

    # Mark symbols that dropped out of today's pull as inactive — but
    # NEVER delete them (master-data invariant). last_seen_at stays
    # frozen on its prior value so we can audit when each dropped.
    if today_symbols:
        cur.execute(
            """
            UPDATE alpaca_tradable_universe
            SET status     = 'inactive',
                tradable   = FALSE,
                shortable  = FALSE,
                updated_at = NOW()
            WHERE symbol <> ALL(%s::text[])
              AND (status = 'active' OR tradable = TRUE)
            """,
            (list(today_symbols),),
        )

    # Append audit row.
    cur.execute(
        """
        INSERT INTO alpaca_tradable_universe_refresh_log
          (total_active, total_tradable, newly_listed, newly_inactive,
           status_changes, alpaca_api_ms, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (total_active, total_tradable, newly_listed, newly_inactive,
         status_changes, None, None),
    )
    conn.commit()

    return {
        'total_active':   total_active,
        'total_tradable': total_tradable,
        'newly_listed':   newly_listed,
        'newly_inactive': newly_inactive,
        'status_changes': status_changes,
        'dry_run':        False,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='Fetch + diff but skip DB writes')
    ap.add_argument('--verbose', action='store_true',
                    help='Print first 10 newly-inactive and newly-listed symbols')
    args = ap.parse_args()

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        _log('POSTGRES_URI not set; aborting')
        return 1

    try:
        assets, api_ms = fetch_alpaca_assets()
    except Exception as e:
        _log(f'alpaca asset list failed: {type(e).__name__}: {e}')
        return 1

    if len(assets) < SUSPICIOUS_FLOOR:
        _log(f'CLI returned {len(assets)} rows (< floor {SUSPICIOUS_FLOOR}); '
             f'refusing to write — likely an auth or upstream issue')
        return 2

    _log(f'fetched {len(assets)} us_equity assets in {api_ms}ms')

    conn = psycopg2.connect(uri)
    try:
        stats = upsert_universe(conn, assets, dry_run=args.dry_run)
    except Exception as e:
        conn.rollback()
        _log(f'upsert failed: {type(e).__name__}: {e}')
        return 1
    finally:
        conn.close()

    flag = ' (DRY-RUN)' if stats['dry_run'] else ''
    _log(f'done{flag} — active={stats["total_active"]} '
         f'tradable={stats["total_tradable"]} '
         f'newly_listed={stats["newly_listed"]} '
         f'newly_inactive={stats["newly_inactive"]} '
         f'status_changes={stats["status_changes"]}')

    if args.verbose and (stats['newly_listed'] or stats['newly_inactive']):
        # Re-fetch the diff for human visibility. Cheap on a 13k-row table.
        conn2 = psycopg2.connect(uri)
        cur = conn2.cursor()
        cur.execute(
            """SELECT symbol FROM alpaca_tradable_universe
               WHERE status = 'inactive' AND updated_at > NOW() - INTERVAL '1 hour'
               ORDER BY symbol LIMIT 10"""
        )
        recent_inactive = [r[0] for r in cur]
        if recent_inactive:
            _log(f'  recent inactive (first 10): {", ".join(recent_inactive)}')
        cur.execute(
            """SELECT symbol FROM alpaca_tradable_universe
               WHERE first_seen_at > NOW() - INTERVAL '1 hour'
               ORDER BY symbol LIMIT 10"""
        )
        recent_listed = [r[0] for r in cur]
        if recent_listed:
            _log(f'  newly listed (first 10): {", ".join(recent_listed)}')
        conn2.close()

    # SP-2 Phase A: Chain ticker_metadata_writer after a successful refresh
    # so the daily snapshot stays fresh. Non-fatal: doctor will detect
    # staleness if this fails.
    import subprocess
    subprocess.run(
        ["python3", "-m", "src.pipeline.run_ticker_metadata_step"],
        check=False,
    )

    return 0


if __name__ == '__main__':
    sys.exit(main())
