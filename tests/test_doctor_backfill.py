"""Tests for SP-2 Phase B doctor backfill checks.

Covers:
  - `_check_backfill_progress` PASS on empty audit
  - `_check_backfill_progress` WARN when a quarantined row exists
  - `_check_backfill_progress` FAIL when quarantined > 100 (data poisoning)
  - `_check_backfill_universe_coverage` runs without crashing on current DB

The fixtures stamp inserted rows with `source_tag='TEST_PHASE_B_DOCTOR'` and
`chunk_key` starting with `TEST:doctor:` so cleanup is collision-safe even if a
prior failed run left stragglers.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from src.maintenance.doctor import (
    _check_backfill_progress,
    _check_backfill_universe_coverage,
)


TEST_SOURCE_TAG = 'TEST_PHASE_B_DOCTOR'


def _have_db() -> bool:
    return bool(os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL'))


def _connect():
    import psycopg2
    uri = (os.environ.get('DATABASE_URL')
           or os.environ.get('POSTGRES_URI'))
    return psycopg2.connect(uri)


def _purge_test_rows():
    """Remove any TEST_PHASE_B_DOCTOR rows from prior runs."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM backfill_audit WHERE source_tag = %s",
            (TEST_SOURCE_TAG,),
        )
        conn.commit()


@pytest.fixture
def clean_audit():
    """Yield with the test-tag rows purged; clean again on teardown."""
    if not _have_db():
        pytest.skip('POSTGRES_URI not set')
    _purge_test_rows()
    yield
    _purge_test_rows()


def test_check_backfill_progress_pass_on_empty_audit(monkeypatch, clean_audit):
    """With no audit rows in last 7d, returns PASS (code=0)."""
    monkeypatch.setattr(
        'src.maintenance.doctor._backfill_audit_status_counts',
        lambda: {},
    )
    code, msg = _check_backfill_progress()
    assert code == 0
    assert 'no backfill activity' in msg


def test_check_backfill_progress_warn_on_quarantined(monkeypatch, clean_audit):
    """With at least one quarantined row in last 7d, returns WARN (code=1)."""
    monkeypatch.setattr(
        'src.maintenance.doctor._backfill_audit_status_counts',
        lambda: {'quarantined': 3, 'validated': 50},
    )
    code, msg = _check_backfill_progress()
    assert code == 1
    assert 'quarantined=3' in msg


def test_check_backfill_progress_fail_on_data_poisoning(monkeypatch, clean_audit):
    """With quarantined > 100, returns FAIL (code=2) as data poisoning signal."""
    monkeypatch.setattr(
        'src.maintenance.doctor._backfill_audit_status_counts',
        lambda: {'quarantined': 250},
    )
    code, msg = _check_backfill_progress()
    assert code == 2
    assert 'quarantined=250' in msg


def test_check_backfill_progress_warn_when_real_quarantined_row(clean_audit):
    """Insert a real quarantined row in backfill_audit and run the check
    end-to-end (no monkeypatch on the audit query). Expect WARN."""
    now = datetime.now(tz=timezone.utc)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backfill_audit
                (target, chunk_key, started_at, ended_at, status,
                 rows_written, source_tag, sha256, error_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            ('prices', 'TEST:doctor:quarantined:1', now, now,
             'quarantined', 0, TEST_SOURCE_TAG, None, 'test-injected'),
        )
        conn.commit()

    code, msg = _check_backfill_progress()
    assert code in (1, 2), f'expected WARN or FAIL with a quarantined row, got code={code} msg={msg}'
    assert 'quarantined' in msg


def test_check_backfill_universe_coverage_does_not_crash(clean_audit):
    """End-to-end smoke: runs against the live DB without raising. The exact
    result depends on current Phase B state (which is fine — we only check
    the function returns a (code, msg) tuple with code ∈ {0, 1})."""
    code, msg = _check_backfill_universe_coverage()
    assert code in (0, 1), f'unexpected code={code} msg={msg}'
    assert isinstance(msg, str) and msg
