"""Tests for scripts/backfill_universe_5y.py (Task 6 scaffolding).

The per-target runners are stubs that raise NotImplementedError. These tests
exercise the harness — argparse, the source-tag safety gate, dispatch,
audit-row helpers, and the Redis client.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import backfill_universe_5y as bf  # noqa: E402

SCRIPT = ROOT / 'scripts' / 'backfill_universe_5y.py'


# ── Subprocess invocation tests ──────────────────────────────────────────────
def _run(*args, env_extra=None):
    """Invoke the driver as a subprocess; returns CompletedProcess."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_driver_refuses_unknown_target():
    """argparse choices= should reject garbage targets with non-zero rc."""
    r = _run('--target', 'garbage')
    assert r.returncode != 0
    # argparse writes choice errors to stderr
    assert 'garbage' in r.stderr or 'invalid choice' in r.stderr


def test_driver_refuses_v2_without_gate():
    """Non-v1 source_tag without the override env must exit rc=2 with REFUSED."""
    env_extra = {}
    # Make sure the gate env is unset for this invocation specifically.
    env_extra['OPENCLAW_BACKFILL_ALLOW_OVERWRITE'] = ''
    r = _run(
        '--target', 'prices',
        '--source-tag', 'backfill_5y_v2',
        '--dry-run',
        env_extra=env_extra,
    )
    assert r.returncode == 2, (
        f'expected rc=2 from safety gate, got rc={r.returncode}\n'
        f'STDOUT: {r.stdout}\nSTDERR: {r.stderr}'
    )
    combined = (r.stderr or '') + (r.stdout or '')
    assert 'REFUSED' in combined


def test_driver_accepts_v2_with_gate():
    """With OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1, the gate must let v2 past.

    Task 9 implemented --target options, so all three runners are now real
    (no more NotImplementedError stub to lean on). We assert the gate is
    bypassed by checking that REFUSED does NOT appear in the output AND that
    the script proceeded into the per-target runner (we look for the runner's
    banner '[options]'). We pass an explicit narrow window so the test
    completes quickly: the runner will issue a few alpaca chain calls then
    quarantine + exit cleanly.
    """
    env_extra = {'OPENCLAW_BACKFILL_ALLOW_OVERWRITE': '1'}
    r = _run(
        '--target', 'options',
        '--source-tag', 'backfill_5y_v2',
        '--tickers', 'SPY',
        '--start-date', '2026-05-12',
        '--end-date', '2026-05-12',
        '--dry-run',
        env_extra=env_extra,
    )
    combined = (r.stderr or '') + (r.stdout or '')
    assert 'REFUSED' not in combined, (
        f'safety gate fired when it should have been bypassed:\n{combined}'
    )
    # The runner printed its banner → we got past the gate. rc may be 0 on
    # a successful dry-run, or non-zero if Alpaca/Redis aren't available
    # — either way, the gate did not fire.
    assert '[options]' in combined, (
        f'expected options runner banner, got:\n{combined}'
    )


# ── Audit-row helper tests (live PG) ─────────────────────────────────────────
@pytest.fixture
def pg_conn():
    """Yields a live psycopg2 connection. Skip if POSTGRES_URI not set."""
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        pytest.skip('POSTGRES_URI not set')
    conn = psycopg2.connect(uri)
    try:
        yield conn
    finally:
        conn.close()


def _cleanup_audit(conn, audit_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute('DELETE FROM backfill_audit WHERE id = %s', (audit_id,))
    conn.commit()


def test_audit_start_inserts_row(pg_conn):
    chunk_key = f'TEST:{uuid.uuid4().hex[:12]}'
    audit_id = bf._audit_start(pg_conn, 'prices', chunk_key, 'backfill_5y_v1')
    try:
        assert isinstance(audit_id, int) and audit_id > 0
        with pg_conn.cursor() as cur:
            cur.execute(
                """SELECT target, chunk_key, status, source_tag, ended_at
                     FROM backfill_audit WHERE id = %s""",
                (audit_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 'prices'
        assert row[1] == chunk_key
        assert row[2] == 'in_progress'
        assert row[3] == 'backfill_5y_v1'
        assert row[4] is None  # ended_at still NULL while in_progress
    finally:
        _cleanup_audit(pg_conn, audit_id)


def test_audit_finish_updates_row(pg_conn):
    chunk_key = f'TEST:{uuid.uuid4().hex[:12]}'
    audit_id = bf._audit_start(pg_conn, 'metadata', chunk_key, 'backfill_5y_v1')
    try:
        bf._audit_finish(
            pg_conn,
            audit_id,
            status='validated',
            rows=123,
            sha='deadbeef' * 8,
            err=None,
        )
        with pg_conn.cursor() as cur:
            cur.execute(
                """SELECT status, ended_at, rows_written, sha256, error_text
                     FROM backfill_audit WHERE id = %s""",
                (audit_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == 'validated'
        assert row[1] is not None  # ended_at populated
        assert row[2] == 123
        assert row[3] == 'deadbeef' * 8
        assert row[4] is None
    finally:
        _cleanup_audit(pg_conn, audit_id)


# ── Redis helper test ────────────────────────────────────────────────────────
def test_redis_helper_returns_usable_client():
    client = bf._redis()
    # _redis already calls .ping() once during construction; do it again to
    # prove the returned client is alive and responds.
    assert client.ping() is True
