#!/usr/bin/env python3
"""SP-2 Phase B — 5-year universe backfill driver.

Scaffold/harness only (Task 6). Per-target implementations land in:
  - prices   : Task 7  (_run_prices)
  - metadata : Task 8  (_run_metadata)
  - options  : Task 9  (_run_options)

Cross-cutting concerns this module owns:
  - argparse surface (resume/dry-run/tickers/years/source-tag/supersede-quarantine)
  - non-v1 source_tag safety gate (env: OPENCLAW_BACKFILL_ALLOW_OVERWRITE)
  - staging + checkpoint directory ensure-exist (kept under data/, gitignored)
  - Redis client helper (matches src/database/datahub.py pattern: URL + ping)
  - Postgres `backfill_audit` row helpers (start/finish)
  - Discord webhook notifier (best-effort, silent failure)
  - top-level dispatch + PG cleanup

Usage:
  POSTGRES_URI=... python3 scripts/backfill_universe_5y.py \
      --target {prices|metadata|options} [--resume] [--dry-run] \
      [--tickers AAPL,MSFT] [--years 2021,2022] \
      [--source-tag backfill_5y_v1] [--supersede-quarantine]
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

import psycopg2


# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / 'data' / '.staging'
CHECKPOINTS = ROOT / 'data' / '.checkpoints' / 'backfill_5y'
UNIVERSE_FILE = ROOT / 'data' / '.backfill_universe_v1.txt'

# Source-tag versioning. Anything other than the canonical v1 tag requires an
# explicit env override so the operator can't accidentally overwrite a clean
# v1 promotion with a re-run under the same dirname.
CANONICAL_SOURCE_TAG = 'backfill_5y_v1'


# ── Argparse ──────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='backfill_universe_5y',
        description='SP-2 Phase B 5-year universe backfill driver.',
    )
    p.add_argument(
        '--target',
        required=True,
        choices=['prices', 'metadata', 'options'],
        help='Which dataset to backfill.',
    )
    p.add_argument(
        '--resume',
        action='store_true',
        default=False,
        help='Skip chunks already marked done in the Redis checkpoint.',
    )
    p.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Plan + audit only; do not write parquet or master tables.',
    )
    p.add_argument(
        '--tickers',
        default=None,
        help='Comma-separated ticker override (default: data/.backfill_universe_v1.txt).',
    )
    p.add_argument(
        '--years',
        default=None,
        help='Comma-separated year override (default: last 5 calendar years).',
    )
    p.add_argument(
        '--source-tag',
        default=CANONICAL_SOURCE_TAG,
        help=f'Tag stamped into backfill_audit + parquet metadata (default: {CANONICAL_SOURCE_TAG}).',
    )
    p.add_argument(
        '--supersede-quarantine',
        action='store_true',
        default=False,
        help='If set, validated rows are allowed to overwrite a previously-quarantined chunk.',
    )
    return p


# ── Safety gate ───────────────────────────────────────────────────────────────
def _check_source_tag_gate(source_tag: str) -> None:
    """Refuse non-v1 source_tag unless operator explicitly opts in via env.

    Exits with rc=2 (config error) so callers / cron can tell this apart from
    a runtime NotImplementedError (rc=1 default) or a missing arg (rc=2 from
    argparse — same code, but the message disambiguates).
    """
    if source_tag == CANONICAL_SOURCE_TAG:
        return
    if os.environ.get('OPENCLAW_BACKFILL_ALLOW_OVERWRITE') == '1':
        return
    sys.stderr.write(
        f'REFUSED: non-v1 source_tag requires OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1 '
        f'(got source_tag={source_tag!r}).\n'
    )
    sys.exit(2)


# ── Directory setup ───────────────────────────────────────────────────────────
def _ensure_dirs() -> None:
    """Create staging + checkpoint dirs. data/ stays gitignored."""
    STAGING.mkdir(parents=True, exist_ok=True)
    CHECKPOINTS.mkdir(parents=True, exist_ok=True)


# ── Redis helper ──────────────────────────────────────────────────────────────
def _redis():
    """Return a Python redis client matching src/database/datahub.py pattern.

    There is no src/database/redis.py (only redis.js for the Node side). The
    canonical Python pattern in the codebase is:
        redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)
    followed by .ping(). The env var is REDIS_URL (not REDIS_URI).
    """
    import redis
    url = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0')
    client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
    client.ping()
    return client


# ── Postgres audit-row helpers ────────────────────────────────────────────────
def _audit_start(pg, target: str, chunk_key: str, source_tag: str) -> int:
    """Insert an in_progress audit row and return its id.

    The (target, chunk_key, source_tag, started_at) UNIQUE constraint means
    parallel runs against the same chunk in the same wallclock instant would
    collide — Redis ops sequencing prevents that in practice. started_at is
    set server-side via NOW() so we don't depend on clock skew.
    """
    with pg.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backfill_audit
                (target, chunk_key, started_at, status, source_tag)
            VALUES (%s, %s, NOW(), 'in_progress', %s)
            RETURNING id
            """,
            (target, chunk_key, source_tag),
        )
        audit_id = cur.fetchone()[0]
    pg.commit()
    return int(audit_id)


def _audit_finish(
    pg,
    audit_id: int,
    status: str,
    rows: int = 0,
    sha: Optional[str] = None,
    err: Optional[str] = None,
) -> None:
    """Mark a previously-started audit row terminal.

    Valid terminal statuses (per migration 115 comments):
        validated, promoted, quarantined, failed
    No validation here — the per-target runners own that contract.
    """
    with pg.cursor() as cur:
        cur.execute(
            """
            UPDATE backfill_audit
               SET status = %s,
                   ended_at = NOW(),
                   rows_written = %s,
                   sha256 = %s,
                   error_text = %s
             WHERE id = %s
            """,
            (status, rows, sha, err, audit_id),
        )
    pg.commit()


# ── Discord notifier ──────────────────────────────────────────────────────────
def _notify_discord(msg: str) -> None:
    """Best-effort POST to DISCORD_BACKFILL_LOG_WEBHOOK. Never raises."""
    url = os.environ.get('DISCORD_BACKFILL_LOG_WEBHOOK')
    if not url:
        return
    try:
        payload = (
            b'{"content":' + __import__('json').dumps(msg)[:1900].encode() + b'}'
        )
        req = urllib.request.Request(
            url,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, OSError, Exception):
        pass  # silent — this is a notifier, not a critical path


# ── Per-target runners (stubs — Tasks 7/8/9) ─────────────────────────────────
def _run_prices(args: argparse.Namespace, pg) -> None:
    """Daily-bar 5y backfill — implemented in Task 7."""
    raise NotImplementedError(
        '_run_prices is implemented in Task 7 (sp2-b prices backfill).'
    )


def _run_metadata(args: argparse.Namespace, pg) -> None:
    """Quarterly metadata snapshot backfill — implemented in Task 8."""
    raise NotImplementedError(
        '_run_metadata is implemented in Task 8 (sp2-b metadata backfill).'
    )


def _run_options(args: argparse.Namespace, pg) -> None:
    """Options-EOD 5y backfill — implemented in Task 9."""
    raise NotImplementedError(
        '_run_options is implemented in Task 9 (sp2-b options backfill).'
    )


_DISPATCH = {
    'prices': _run_prices,
    'metadata': _run_metadata,
    'options': _run_options,
}


# ── Entrypoint ───────────────────────────────────────────────────────────────
def main(argv: Optional[list] = None) -> None:
    args = _build_parser().parse_args(argv)

    # Pure-env safety gate runs before any IO so tests can assert rc=2 without
    # a live Postgres.
    _check_source_tag_gate(args.source_tag)

    _ensure_dirs()

    pg = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        runner = _DISPATCH[args.target]
        runner(args, pg)
    finally:
        try:
            pg.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
