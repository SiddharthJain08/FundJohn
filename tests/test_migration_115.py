# tests/test_migration_115.py
#
# SP-2 Phase B Task 3: backfill_audit table.
# Append-only durable audit log of every backfill chunk attempt.

import os
import psycopg2
import pytest

DSN = os.environ["POSTGRES_URI"]


def test_table_exists():
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SELECT to_regclass('public.backfill_audit')")
        assert cur.fetchone()[0] == "backfill_audit"


def test_columns_present_with_correct_types():
    expected = {
        "id": ("bigint", "NO"),
        "target": ("text", "NO"),
        "chunk_key": ("text", "NO"),
        "started_at": ("timestamp with time zone", "NO"),
        "ended_at": ("timestamp with time zone", "YES"),
        "status": ("text", "NO"),
        "rows_written": ("integer", "YES"),
        "source_tag": ("text", "NO"),
        "sha256": ("text", "YES"),
        "error_text": ("text", "YES"),
    }
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name='backfill_audit'
            """
        )
        got = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    for col, (dtype, nn) in expected.items():
        assert col in got, f"missing column {col}"
        assert got[col][0] == dtype, f"{col}: type {got[col][0]} != expected {dtype}"
        assert got[col][1] == nn, f"{col}: nullability {got[col][1]} != expected {nn}"


def test_both_indices_exist():
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename='backfill_audit' ORDER BY 1
            """
        )
        names = [r[0] for r in cur.fetchall()]
    assert "idx_backfill_audit_status" in names
    assert "idx_backfill_audit_recent" in names
    assert "backfill_audit_pkey" in names
    assert "backfill_audit_chunk_unique" in names


def test_unique_constraint_enforces_chunk_uniqueness():
    """Inserting two rows with the same (target, chunk_key, source_tag, started_at)
    must raise UniqueViolation. Differing on any one of those fields must be allowed."""
    started = "2026-05-22 12:34:56+00"
    with psycopg2.connect(DSN) as c, c.cursor() as cur:
        try:
            # First insert — should succeed.
            cur.execute(
                """
                INSERT INTO backfill_audit
                  (target, chunk_key, started_at, status, source_tag)
                VALUES ('prices', 'AAPL:2022', %s, 'in_progress', 'test_v115')
                """,
                (started,),
            )

            # Duplicate (target, chunk_key, source_tag, started_at) -> must fail.
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO backfill_audit
                      (target, chunk_key, started_at, status, source_tag)
                    VALUES ('prices', 'AAPL:2022', %s, 'validated', 'test_v115')
                    """,
                    (started,),
                )
            c.rollback()

            # Differing source_tag at same started_at -> must succeed.
            cur.execute(
                """
                INSERT INTO backfill_audit
                  (target, chunk_key, started_at, status, source_tag)
                VALUES ('prices', 'AAPL:2022', %s, 'in_progress', 'test_v115')
                """,
                (started,),
            )
            cur.execute(
                """
                INSERT INTO backfill_audit
                  (target, chunk_key, started_at, status, source_tag)
                VALUES ('prices', 'AAPL:2022', %s, 'in_progress', 'test_v115_alt')
                """,
                (started,),
            )

            # Differing started_at -> must succeed.
            cur.execute(
                """
                INSERT INTO backfill_audit
                  (target, chunk_key, started_at, status, source_tag)
                VALUES ('prices', 'AAPL:2022', '2026-05-22 12:34:57+00', 'in_progress', 'test_v115')
                """
            )
        finally:
            c.rollback()
            with c.cursor() as cleanup:
                cleanup.execute(
                    "DELETE FROM backfill_audit WHERE source_tag IN ('test_v115','test_v115_alt')"
                )
            c.commit()
