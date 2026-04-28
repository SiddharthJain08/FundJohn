# Provider-specific historical-backfill adapters.
#
# Each module exports:
#     def backfill(column_name: str, from_date: date, to_date: date) -> int:
# returning the number of rows written to master parquets / DB tables.
# Raise on unrecoverable errors; the caller marks backfill_status='failed'.
"""Backfill orchestration helpers.

Two callers consume this package:

  1. ``src/pipeline/queue_drain.py`` — the daily-cycle step 1 batch drainer.
  2. ``src/agent/approvals/staging_approver.js`` — the fused staging-approval
     worker (via ``src/lib/backfill_runner.js`` Node wrapper). Operator clicks
     Approve on a staging strategy and the worker calls
     ``backfill_one_request(request_id)`` per missing column inline (no waiting
     for tomorrow's queue_drain step).

Both consumers must produce identical state transitions on the queue row +
``data_columns`` ledger so the daily collector picks the column up next cycle
without further intervention. The helper below is the single canonical path.
"""
from __future__ import annotations

import importlib
import json
import os
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

OPENCLAW_DIR = Path(os.environ.get('OPENCLAW_DIR', '/root/openclaw'))
SCHEMA_REGISTRY_PATH = OPENCLAW_DIR / 'data' / 'master' / 'schema_registry.json'

_BACKFILLER_MODULES = {
    'fmp':          'src.pipeline.backfillers.fmp',
    'polygon':      'src.pipeline.backfillers.polygon',
    'yfinance':     'src.pipeline.backfillers.yfinance',
    'edgar':        'src.pipeline.backfillers.edgar',
    'tavily':       'src.pipeline.backfillers.tavily',
}


def _load_registry() -> dict:
    if not SCHEMA_REGISTRY_PATH.exists():
        return {}
    return json.loads(SCHEMA_REGISTRY_PATH.read_text())


def _resolve_provider(cur, column_name: str, hint: Optional[str]) -> Optional[str]:
    """Look up the provider for a column. Preference order:
       1. Explicit hint from the queue row (provider_preferred)
       2. data_columns ledger
       3. schema_registry.json (search datasets whose columns list contains the name)
    """
    if hint:
        return hint
    cur.execute("SELECT provider FROM data_columns WHERE column_name = %s", (column_name,))
    r = cur.fetchone()
    if r and r[0]:
        return r[0]
    reg = _load_registry()
    for dataset, meta in reg.items():
        cols = meta.get('columns') or []
        if column_name in cols or dataset == column_name:
            return meta.get('provider')
    return None


def _import_backfiller(provider: str):
    mod_name = _BACKFILLER_MODULES.get(provider)
    if not mod_name:
        raise ValueError(f'No backfiller registered for provider={provider!r}')
    return importlib.import_module(mod_name)


def backfill_one_request(request_id: str, *, dry_run: bool = False) -> dict:
    """Backfill one row of ``data_ingestion_queue``.

    Single canonical path used by both ``queue_drain.py`` and the Node-side
    ``staging_approver.js`` (via ``backfill_runner.js``).

    Returns a JSON-serialisable dict:
        {
          'ok': bool,
          'request_id': str,
          'column_name': str,
          'provider': str | None,
          'rows_written': int,
          'elapsed_s': float,
          'from_date': str,
          'to_date': str,
          'error': str | None,
        }

    Side effects (when not dry-run):
      - Updates the queue row (backfill_status, rows_backfilled, wired_at, etc.)
      - Ensures ``data_columns`` has a row so the next daily collector cycle
        picks the column up.
    """
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return {
            'ok': False, 'request_id': request_id, 'column_name': None,
            'provider': None, 'rows_written': 0, 'elapsed_s': 0.0,
            'from_date': None, 'to_date': None,
            'error': 'POSTGRES_URI not set',
        }

    conn = psycopg2.connect(uri)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT request_id, column_name, provider_preferred, provider_fallback,
                   strategy_id, backfill_from, backfill_to, backfill_status, status
              FROM data_ingestion_queue
             WHERE request_id = %s
            """,
            (request_id,),
        )
        row = cur.fetchone()
        if not row:
            return {
                'ok': False, 'request_id': request_id, 'column_name': None,
                'provider': None, 'rows_written': 0, 'elapsed_s': 0.0,
                'from_date': None, 'to_date': None,
                'error': f'no data_ingestion_queue row {request_id!r}',
            }

        col = row['column_name']
        # Resolve provider via plain cursor (RealDictCursor iter is consumed).
        with conn.cursor() as plain_cur:
            provider = _resolve_provider(plain_cur, col, row['provider_preferred'])

        frm = row['backfill_from'] or (date.today() - timedelta(days=1825))
        to  = row['backfill_to']   or date.today()

        if dry_run:
            return {
                'ok': True, 'request_id': str(request_id), 'column_name': col,
                'provider': provider, 'rows_written': 0, 'elapsed_s': 0.0,
                'from_date': str(frm), 'to_date': str(to), 'error': None,
            }

        if not provider:
            cur.execute(
                """UPDATE data_ingestion_queue
                     SET backfill_status='failed', backfill_error=%s,
                         backfill_finished_at=NOW()
                   WHERE request_id=%s""",
                ('No provider mapping — update schema_registry.json', request_id),
            )
            conn.commit()
            return {
                'ok': False, 'request_id': str(request_id), 'column_name': col,
                'provider': None, 'rows_written': 0, 'elapsed_s': 0.0,
                'from_date': str(frm), 'to_date': str(to),
                'error': 'No provider registered for column',
            }

        import time as _time
        t0 = _time.monotonic()
        try:
            cur.execute(
                """UPDATE data_ingestion_queue
                     SET backfill_status='running', backfill_started_at=NOW()
                   WHERE request_id=%s""",
                (request_id,),
            )
            conn.commit()

            bf = _import_backfiller(provider)
            rows_written = bf.backfill(column_name=col, from_date=frm, to_date=to) or 0
            elapsed = _time.monotonic() - t0

            cur.execute(
                """UPDATE data_ingestion_queue
                     SET backfill_status='complete',
                         backfill_finished_at=NOW(),
                         rows_backfilled=%s,
                         wired_at=NOW()
                   WHERE request_id=%s""",
                (int(rows_written), request_id),
            )
            # Register the column in the data_columns ledger so the next daily
            # collector cycle picks it up automatically. This is the
            # "after-backfill the column joins the daily collection set"
            # invariant that staging-approval depends on.
            cur.execute(
                """INSERT INTO data_columns (column_name, provider, refresh_cadence)
                        VALUES (%s, %s, 'daily')
                    ON CONFLICT (column_name) DO UPDATE
                      SET provider = EXCLUDED.provider""",
                (col, provider),
            )
            conn.commit()

            return {
                'ok': True, 'request_id': str(request_id), 'column_name': col,
                'provider': provider, 'rows_written': int(rows_written),
                'elapsed_s': round(elapsed, 2),
                'from_date': str(frm), 'to_date': str(to), 'error': None,
            }
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            cur.execute(
                """UPDATE data_ingestion_queue
                     SET backfill_status='failed',
                         backfill_error=%s,
                         backfill_finished_at=NOW()
                   WHERE request_id=%s""",
                (f'{type(e).__name__}: {e}\n{tb}'[:1500], request_id),
            )
            conn.commit()
            return {
                'ok': False, 'request_id': str(request_id), 'column_name': col,
                'provider': provider, 'rows_written': 0,
                'elapsed_s': round(_time.monotonic() - t0, 2),
                'from_date': str(frm), 'to_date': str(to),
                'error': f'{type(e).__name__}: {e}',
            }
    finally:
        conn.close()


def _cli() -> int:
    """python3 -m src.pipeline.backfillers <request_id>  → JSON to stdout."""
    import sys
    if len(sys.argv) < 2:
        print(json.dumps({'ok': False, 'error': 'usage: backfillers <request_id> [--dry-run]'}))
        return 2
    rid = sys.argv[1]
    dry = '--dry-run' in sys.argv[2:]
    out = backfill_one_request(rid, dry_run=dry)
    print(json.dumps(out, default=str))
    return 0 if out.get('ok') else 1


if __name__ == '__main__':
    import sys
    sys.exit(_cli())
