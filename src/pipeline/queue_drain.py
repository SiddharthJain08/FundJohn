#!/usr/bin/env python3
"""
queue_drain.py — first step of the new daily-cycle orchestrator.

Drains two queues per cycle:

  1. data_ingestion_queue (status='APPROVED', backfill_status in pending/failed)
       → dispatch to the appropriate backfiller; update backfill_status as it
         goes. A successfully backfilled column joins the daily collection
         set by virtue of being referenced in data/master/schema_registry.json.

  2. data_deprecation_queue (status='APPROVED', deletion_applied_at IS NULL)
       → remove the column from schema_registry.json and from the data_columns
         ledger row (historical parquet data is preserved). Sets
         deletion_applied_at so the row is not re-processed.

Usage:
  python3 src/pipeline/queue_drain.py [--date YYYY-MM-DD] [--dry-run]

Environment: POSTGRES_URI, OPENCLAW_DIR. Run from the pipeline_orchestrator.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / '.env')
except ImportError:
    pass

OPENCLAW_DIR = Path(os.environ.get('OPENCLAW_DIR', '/root/openclaw'))
SCHEMA_REGISTRY_PATH = OPENCLAW_DIR / 'data' / 'master' / 'schema_registry.json'

# Lazily import backfillers on demand so a missing provider doesn't crash the
# drainer on unrelated rows.
_BACKFILLER_MODULES = {
    'fmp':          'src.pipeline.backfillers.fmp',
    'polygon':      'src.pipeline.backfillers.polygon',
    'yfinance':     'src.pipeline.backfillers.yfinance',
    'edgar':        'src.pipeline.backfillers.edgar',
    'tavily':       'src.pipeline.backfillers.tavily',
    'alphavantage': 'src.pipeline.backfillers.alphavantage',
}


def log(msg: str) -> None:
    ts = datetime.utcnow().strftime('%H:%M:%S')
    print(f'[{ts}] [queue_drain] {msg}', flush=True)


# ── Discord #data-alerts (Phase 4) ─────────────────────────────────────────
# Concise progress for the operator. Uses DATABOT_TOKEN REST call, cached
# channel ID per process. No-op if the token is unset.

_DATA_ALERTS_WEBHOOK: str | None = None

def _data_alerts_webhook() -> str:
    """Load the DataBot `data-alerts` webhook URL from agent_registry.
    Posting via webhook URL bypasses channel-level bot role permissions
    (the bot-token path 403s on every channel in this server)."""
    global _DATA_ALERTS_WEBHOOK
    if _DATA_ALERTS_WEBHOOK is not None:
        return _DATA_ALERTS_WEBHOOK
    try:
        conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        cur = conn.cursor()
        cur.execute("SELECT webhook_urls FROM agent_registry WHERE id='databot'")
        row = cur.fetchone()
        conn.close()
        _DATA_ALERTS_WEBHOOK = ((row[0] if row else {}) or {}).get('data-alerts', '') or ''
    except Exception as e:
        print(f'[queue_drain] webhook lookup failed: {e}')
        _DATA_ALERTS_WEBHOOK = ''
    return _DATA_ALERTS_WEBHOOK


def data_alert(msg: str) -> None:
    url = _data_alerts_webhook()
    if not url:
        return
    import requests as _rq
    try:
        _rq.post(url, json={'content': msg[:1900]}, timeout=5)
    except Exception as e:
        print(f'[queue_drain] data-alert post failed: {e}')


def load_registry() -> dict:
    if not SCHEMA_REGISTRY_PATH.exists():
        return {}
    return json.loads(SCHEMA_REGISTRY_PATH.read_text())


def save_registry(reg: dict) -> None:
    SCHEMA_REGISTRY_PATH.write_text(json.dumps(reg, indent=2) + '\n')


def resolve_provider(cur, column_name: str, hint: str | None) -> str | None:
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
    reg = load_registry()
    for dataset, meta in reg.items():
        cols = meta.get('columns') or []
        if column_name in cols or dataset == column_name:
            return meta.get('provider')
    return None


def import_backfiller(provider: str):
    mod_name = _BACKFILLER_MODULES.get(provider)
    if not mod_name:
        raise ValueError(f'No backfiller registered for provider={provider!r}')
    import importlib
    return importlib.import_module(mod_name)


def drain_ingestion(conn, dry_run: bool) -> dict:
    """Process APPROVED rows with pending/failed backfill_status."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT request_id, column_name, provider_preferred, provider_fallback,
               strategy_id, backfill_from, backfill_to, backfill_status
          FROM data_ingestion_queue
         WHERE status = 'APPROVED'
           AND (backfill_status IS NULL
                OR backfill_status IN ('pending','failed'))
         ORDER BY priority DESC, requested_at ASC
        """
    )
    rows = cur.fetchall()
    log(f'{len(rows)} row(s) pending in data_ingestion_queue')

    summary = {'processed': 0, 'succeeded': 0, 'failed': 0, 'skipped': 0}
    for row in rows:
        rid = row['request_id']
        col = row['column_name']
        provider = resolve_provider(conn.cursor(), col, row['provider_preferred'])
        if not provider:
            log(f'[SKIP] {col}: no provider registered')
            if not dry_run:
                cur.execute(
                    """UPDATE data_ingestion_queue
                         SET backfill_status='failed', backfill_error=%s,
                             backfill_finished_at=NOW()
                       WHERE request_id=%s""",
                    ('No provider mapping — update schema_registry.json', rid),
                )
                conn.commit()
            summary['skipped'] += 1
            continue

        # Default backfill window: 5 years history if not set.
        frm = row['backfill_from'] or (date.today() - timedelta(days=1825))
        to  = row['backfill_to']   or date.today()

        if dry_run:
            log(f'[DRY] would backfill {col} via {provider} from {frm} to {to}')
            summary['processed'] += 1
            continue

        import time as _time
        t0 = _time.monotonic()
        try:
            days_span = (to - frm).days if hasattr(to, '__sub__') else 0
            log(f'[RUN] {col} via {provider} — window {frm}→{to} ({days_span}d)')
            data_alert(
                f'📥 **Backfill started** — `{col}` via `{provider}`\n'
                f'• Range: {frm} → {to} ({days_span} days, ~{days_span//365}y history)\n'
                f'• Strategy requester: `{row.get("strategy_id") or "—"}`'
            )
            cur.execute(
                """UPDATE data_ingestion_queue
                     SET backfill_status='running', backfill_started_at=NOW()
                   WHERE request_id=%s""", (rid,),
            )
            conn.commit()
            bf = import_backfiller(provider)
            rows_written = bf.backfill(column_name=col, from_date=frm, to_date=to) or 0
            elapsed = _time.monotonic() - t0
            cur.execute(
                """UPDATE data_ingestion_queue
                     SET backfill_status='complete',
                         backfill_finished_at=NOW(),
                         rows_backfilled=%s,
                         wired_at=NOW()
                   WHERE request_id=%s""",
                (int(rows_written), rid),
            )
            # Also ensure data_columns has a row so future drains know the provider.
            cur.execute(
                """INSERT INTO data_columns (column_name, provider, refresh_cadence)
                        VALUES (%s, %s, 'daily')
                    ON CONFLICT (column_name) DO UPDATE
                      SET provider = EXCLUDED.provider""",
                (col, provider),
            )
            conn.commit()
            summary['succeeded'] += 1
            summary.setdefault('detail', []).append({
                'column': col, 'provider': provider, 'rows': int(rows_written),
                'elapsed_s': round(elapsed, 1), 'from': str(frm), 'to': str(to),
            })
            log(f'[OK]  {col}: {rows_written:,} rows in {elapsed:.1f}s')
            data_alert(
                f'✅ **Backfill complete** — `{col}`\n'
                f'• Rows written: **{rows_written:,}** via `{provider}`\n'
                f'• Elapsed: **{elapsed:.1f}s**\n'
                f'• Column is now joining the daily collection set.'
            )
        except Exception as e:
            _ = _time.monotonic() - t0  # swallowed; elapsed is in log line below
            tb = traceback.format_exc(limit=3)
            log(f'[FAIL] {col}: {e}')
            data_alert(f'❌ `{col}` backfill FAILED — `{type(e).__name__}: {str(e)[:200]}`')
            cur.execute(
                """UPDATE data_ingestion_queue
                     SET backfill_status='failed',
                         backfill_error=%s,
                         backfill_finished_at=NOW()
                   WHERE request_id=%s""",
                (f'{type(e).__name__}: {e}\n{tb}'[:1500], rid),
            )
            conn.commit()
            summary['failed'] += 1
        summary['processed'] += 1
    return summary


def drain_deprecation(conn, dry_run: bool) -> dict:
    """Apply approved column removals: drop from schema_registry + data_columns."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT request_id, column_name, last_used_by, recommended_action
             FROM data_deprecation_queue
            WHERE status='APPROVED' AND deletion_applied_at IS NULL
         ORDER BY deprecated_at ASC"""
    )
    rows = cur.fetchall()
    log(f'{len(rows)} row(s) pending in data_deprecation_queue')
    summary = {'processed': 0, 'removed': 0, 'failed': 0}

    reg = load_registry()
    changed = False
    for row in rows:
        rid = row['request_id']
        col = row['column_name']
        if dry_run:
            log(f'[DRY] would remove {col} from registry/data_columns')
            summary['processed'] += 1
            continue
        try:
            # Remove from every dataset's columns list; drop empty datasets too.
            for dataset, meta in list(reg.items()):
                cols = meta.get('columns') or []
                if col in cols:
                    meta['columns'] = [c for c in cols if c != col]
                    changed = True
                if dataset == col:
                    # dataset-level removal
                    del reg[dataset]
                    changed = True
            cur.execute("DELETE FROM data_columns WHERE column_name=%s", (col,))
            cur.execute(
                """UPDATE data_deprecation_queue
                     SET deletion_applied_at=NOW()
                   WHERE request_id=%s""", (rid,),
            )
            conn.commit()
            summary['removed'] += 1
            log(f'[OK]  removed {col} from live collection')
        except Exception as e:
            cur.execute(
                """UPDATE data_deprecation_queue
                     SET deletion_error=%s
                   WHERE request_id=%s""",
                (f'{type(e).__name__}: {e}'[:800], rid),
            )
            conn.commit()
            summary['failed'] += 1
            log(f'[FAIL] {col}: {e}')
        summary['processed'] += 1
    if changed and not dry_run:
        save_registry(reg)
        log('schema_registry.json updated')
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()),
                    help='Cycle date (recorded in logs; not used for fetches).')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print the dispatch plan; make no DB writes.')
    args = ap.parse_args()

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        log('POSTGRES_URI not set — aborting')
        return 1
    import time as _time
    conn = psycopg2.connect(uri)
    t0 = _time.monotonic()
    try:
        log(f'Cycle date {args.date}')
        ingest  = drain_ingestion(conn, args.dry_run)
        depr    = drain_deprecation(conn, args.dry_run)
        elapsed = _time.monotonic() - t0
        log(f'Summary — ingest: {ingest} | deprecation: {depr}')

        # Post a single descriptive summary to #data-alerts only when there
        # was actual work (rows processed on either queue). Idle ticks
        # stay silent — the orchestrator's #pipeline-feed line already
        # shows the step ran.
        total = ingest.get('processed', 0) + depr.get('processed', 0)
        if total > 0:
            detail_lines = []
            for d in ingest.get('detail', []):
                detail_lines.append(
                    f"• `{d['column']}` — {d['rows']:,} rows via `{d['provider']}` ({d['from']}→{d['to']}) in {d['elapsed_s']}s"
                )
            msg = (
                f"📊 **Queue-drain complete** — {args.date}\n"
                f"• Ingest: {ingest.get('succeeded',0)} ok / {ingest.get('failed',0)} failed / {ingest.get('skipped',0)} skipped\n"
                f"• Deprecation: {depr.get('removed',0)} removed / {depr.get('failed',0)} failed\n"
                f"• Total elapsed: {elapsed:.1f}s"
            )
            if detail_lines:
                msg += '\n' + '\n'.join(detail_lines[:10])
            data_alert(msg)
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
