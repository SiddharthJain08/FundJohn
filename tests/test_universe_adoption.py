"""tests/test_universe_adoption.py — TDD tests for SP-2 Phase C Task 5.

Tests:
  1. adopt writes DB (approved+adopted=true) AND manifest AND an audit row.
  2. After adopt, the resolver loads the adopted predicate without error.
  3. adopt on an already-decided rec raises ValueError.
  4. revert restores the prior ref and writes a revert audit row.
  5. list_pending_recommendations returns only approved IS NULL rows.
  6. Manifest safety: OPENCLAW_MANIFEST_PATH → tmp copy; real manifest never touched.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategies.universe_default import large_cap, CANDIDATE_PREDICATES
from src.strategies.universe_resolver import UniverseResolver
from src.strategies.universe_meta import TickerMetadata


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

POSTGRES_URI = os.environ.get("POSTGRES_URI")


def _skip_if_no_db():
    if not POSTGRES_URI:
        pytest.skip("POSTGRES_URI not set")


@pytest.fixture()
def pg_conn():
    """Raw psycopg2 connection for test setup/teardown."""
    _skip_if_no_db()
    conn = psycopg2.connect(POSTGRES_URI)
    conn.autocommit = False
    yield conn
    conn.close()


@pytest.fixture()
def tmp_manifest(tmp_path):
    """Copy the real manifest to a temp location; set OPENCLAW_MANIFEST_PATH;
    yield the path; restore env after test."""
    real = ROOT / "src" / "strategies" / "manifest.json"
    tmp = tmp_path / "manifest.json"
    shutil.copy(real, tmp)
    prev = os.environ.get("OPENCLAW_MANIFEST_PATH")
    os.environ["OPENCLAW_MANIFEST_PATH"] = str(tmp)
    yield tmp
    # Restore
    if prev is None:
        os.environ.pop("OPENCLAW_MANIFEST_PATH", None)
    else:
        os.environ["OPENCLAW_MANIFEST_PATH"] = prev


@pytest.fixture()
def test_strategy_id():
    """Unique synthetic strategy_id so tests never collide with real data."""
    return f"S_test_adopt_{uuid.uuid4().hex[:8]}"


def _insert_rec(conn, strategy_id: str, candidate: str = "large_cap") -> int:
    """Insert a pending recommendation; return rec_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO strategy_universe_recommendations
              (strategy_id, candidate_predicate, candidate_set_id, backtest_summary)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (
                strategy_id,
                candidate,
                "test_set_v1",
                json.dumps({"sharpe": 1.2, "max_dd": 0.08, "trades": 42, "mean_universe_size": 150}),
            ),
        )
        rec_id = cur.fetchone()[0]
    conn.commit()
    return rec_id


def _cleanup(conn, strategy_id: str, rec_ids: list[int]) -> None:
    """Delete only the test rows created by this test."""
    with conn.cursor() as cur:
        if rec_ids:
            cur.execute(
                "DELETE FROM strategy_universe_recommendations WHERE id = ANY(%s)",
                (rec_ids,),
            )
        cur.execute(
            "DELETE FROM lifecycle_audit_log WHERE strategy_id = %s",
            (strategy_id,),
        )
    conn.commit()


def _ensure_strategy_in_manifest(manifest_path: Path, strategy_id: str) -> None:
    """Add a minimal strategy entry to the tmp manifest if not present."""
    with open(manifest_path) as f:
        m = json.load(f)
    if strategy_id not in m.get("strategies", {}):
        m.setdefault("strategies", {})[strategy_id] = {
            "state": "live",
            "state_since": "2026-01-01T00:00:00+00:00",
            "metadata": {
                "canonical_file": f"{strategy_id}.py",
                "class": strategy_id,
                "description": "Test strategy",
            },
            "history": [],
        }
        with open(manifest_path, "w") as f:
            json.dump(m, f, indent=2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_adopt_writes_db_and_manifest_and_audit(pg_conn, tmp_manifest, test_strategy_id):
    """Adopt writes: DB rec approved+adopted=true, manifest ref, audit row."""
    _ensure_strategy_in_manifest(tmp_manifest, test_strategy_id)
    rec_id = _insert_rec(pg_conn, test_strategy_id, "large_cap")

    try:
        from src.strategies.lifecycle_universe_adoption import adopt_universe_recommendation

        result = adopt_universe_recommendation(rec_id, actor="test_runner")

        # --- DB assertions ---
        with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT approved, adopted, approved_by FROM strategy_universe_recommendations WHERE id = %s",
                (rec_id,),
            )
            row = cur.fetchone()
        assert row["approved"] is True, "approved should be TRUE after adoption"
        assert row["adopted"] is True, "adopted should be TRUE after adoption"
        assert row["approved_by"] == "test_runner"

        # --- Manifest assertions ---
        with open(tmp_manifest) as f:
            manifest = json.load(f)
        ref = manifest["strategies"][test_strategy_id]["metadata"]["universe_filter_ref"]
        assert ref == "src.strategies.universe_default:large_cap", (
            f"Expected module:attr ref, got: {ref!r}"
        )

        # --- Audit row assertions ---
        with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT event, strategy_id, after_state, actor
                FROM lifecycle_audit_log
                WHERE strategy_id = %s AND event = 'universe_filter_adopted'
                ORDER BY occurred_at DESC LIMIT 1
                """,
                (test_strategy_id,),
            )
            audit = cur.fetchone()
        assert audit is not None, "Audit row should exist after adoption"
        assert audit["after_state"] == "src.strategies.universe_default:large_cap"
        assert audit["actor"] == "test_runner"

        # --- Return value ---
        assert result["strategy_id"] == test_strategy_id
        assert result["after_ref"] == "src.strategies.universe_default:large_cap"

    finally:
        _cleanup(pg_conn, test_strategy_id, [rec_id])


def test_adopt_resolver_loads_predicate(pg_conn, tmp_manifest, test_strategy_id):
    """After adopt, UniverseResolver._load_predicate returns the adopted predicate function."""
    _ensure_strategy_in_manifest(tmp_manifest, test_strategy_id)
    rec_id = _insert_rec(pg_conn, test_strategy_id, "large_cap")

    try:
        from src.strategies.lifecycle_universe_adoption import adopt_universe_recommendation

        adopt_universe_recommendation(rec_id, actor="test_runner")

        # Build resolver with the post-adoption manifest.
        def manifest_loader():
            with open(tmp_manifest) as f:
                return json.load(f)

        class _StubDB:
            def fetch_metadata_as_of(self, as_of):
                return []

        class _StubCoverage:
            def has_floor(self, symbol, as_of):
                return True

        resolver = UniverseResolver(
            db=_StubDB(),
            coverage=_StubCoverage(),
            manifest_loader=manifest_loader,
        )

        predicate = resolver._load_predicate(test_strategy_id)

        # Verify it is the correct function (identity check).
        assert predicate is large_cap, (
            f"Expected large_cap predicate, got {predicate!r}"
        )

        # Verify it can be called without error.
        sample = TickerMetadata(
            symbol="AAPL", asset_class="us_equity", exchange="NASDAQ",
            status="active", tradable=True, shortable=True,
            fractionable=True, easy_to_borrow=True,
            market_cap=3.5e12, adv_usd_20d=1.8e10,
            sector="IT", industry="CE",
            options_eligible=True, in_sp500=True, in_r1000=True, in_r3000=True,
            listed_date=date(1980, 12, 12), delisted_date=None,
        )
        assert predicate(sample, date(2026, 5, 24)) is True

    finally:
        _cleanup(pg_conn, test_strategy_id, [rec_id])


def test_adopt_raises_if_already_decided(pg_conn, tmp_manifest, test_strategy_id):
    """Calling adopt on an already-decided rec raises ValueError."""
    _ensure_strategy_in_manifest(tmp_manifest, test_strategy_id)
    rec_id = _insert_rec(pg_conn, test_strategy_id, "r1000")

    try:
        from src.strategies.lifecycle_universe_adoption import adopt_universe_recommendation

        # First adoption succeeds.
        adopt_universe_recommendation(rec_id, actor="test_runner")

        # Second attempt must raise.
        with pytest.raises(ValueError, match="already decided"):
            adopt_universe_recommendation(rec_id, actor="test_runner")

    finally:
        _cleanup(pg_conn, test_strategy_id, [rec_id])


def test_revert_restores_prior_ref_and_writes_audit(pg_conn, tmp_manifest, test_strategy_id):
    """Revert restores the manifest's universe_filter_ref to before_state and inserts a revert audit row."""
    # Set up: strategy has an existing ref before adoption.
    _ensure_strategy_in_manifest(tmp_manifest, test_strategy_id)
    # Write a prior ref into the tmp manifest.
    with open(tmp_manifest) as f:
        manifest = json.load(f)
    manifest["strategies"][test_strategy_id]["metadata"]["universe_filter_ref"] = (
        "src.strategies.universe_default:sp500"
    )
    with open(tmp_manifest, "w") as f:
        json.dump(manifest, f, indent=2)

    rec_id = _insert_rec(pg_conn, test_strategy_id, "r1000")

    try:
        from src.strategies.lifecycle_universe_adoption import (
            adopt_universe_recommendation,
            revert_universe_recommendation,
        )

        adopt_universe_recommendation(rec_id, actor="test_runner")

        # Confirm manifest was updated.
        with open(tmp_manifest) as f:
            m = json.load(f)
        assert m["strategies"][test_strategy_id]["metadata"]["universe_filter_ref"] == (
            "src.strategies.universe_default:r1000"
        )

        # Now revert.
        results = revert_universe_recommendation(strategy_id=test_strategy_id, actor="test_reverter")

        assert len(results) == 1
        assert results[0]["strategy_id"] == test_strategy_id
        assert results[0]["after_ref"] == "src.strategies.universe_default:sp500"

        # Manifest should be restored to sp500.
        with open(tmp_manifest) as f:
            m = json.load(f)
        restored = m["strategies"][test_strategy_id]["metadata"].get("universe_filter_ref")
        assert restored == "src.strategies.universe_default:sp500", (
            f"Expected sp500 ref after revert, got {restored!r}"
        )

        # Audit row for revert should exist.
        with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT event, before_state, after_state, actor
                FROM lifecycle_audit_log
                WHERE strategy_id = %s AND event = 'universe_filter_reverted'
                ORDER BY occurred_at DESC LIMIT 1
                """,
                (test_strategy_id,),
            )
            revert_audit = cur.fetchone()

        assert revert_audit is not None, "Revert audit row should exist"
        assert revert_audit["before_state"] == "src.strategies.universe_default:r1000"
        assert revert_audit["after_state"] == "src.strategies.universe_default:sp500"
        assert revert_audit["actor"] == "test_reverter"

    finally:
        _cleanup(pg_conn, test_strategy_id, [rec_id])


def test_revert_removes_key_when_no_prior_ref(pg_conn, tmp_manifest, test_strategy_id):
    """Revert removes universe_filter_ref when before_state was None (no prior ref)."""
    # Strategy has no prior universe_filter_ref.
    _ensure_strategy_in_manifest(tmp_manifest, test_strategy_id)

    rec_id = _insert_rec(pg_conn, test_strategy_id, "mid_cap")

    try:
        from src.strategies.lifecycle_universe_adoption import (
            adopt_universe_recommendation,
            revert_universe_recommendation,
        )

        adopt_universe_recommendation(rec_id, actor="test_runner")
        revert_universe_recommendation(strategy_id=test_strategy_id, actor="test_reverter")

        with open(tmp_manifest) as f:
            m = json.load(f)
        meta = m["strategies"][test_strategy_id].get("metadata", {})
        assert "universe_filter_ref" not in meta, (
            "universe_filter_ref should be removed when reverting to no-prior-ref state"
        )

    finally:
        _cleanup(pg_conn, test_strategy_id, [rec_id])


def test_list_pending_returns_only_unapproved(pg_conn, tmp_manifest, test_strategy_id):
    """list_pending_recommendations returns only rows where approved IS NULL."""
    _ensure_strategy_in_manifest(tmp_manifest, test_strategy_id)

    # Insert two recs.
    rec_id_pending = _insert_rec(pg_conn, test_strategy_id, "r3000")
    rec_id_to_adopt = _insert_rec(pg_conn, test_strategy_id, "sp500")

    try:
        from src.strategies.lifecycle_universe_adoption import (
            adopt_universe_recommendation,
            list_pending_recommendations,
        )

        # Adopt one of them.
        adopt_universe_recommendation(rec_id_to_adopt, actor="test_runner")

        # list_pending should contain rec_id_pending but NOT rec_id_to_adopt.
        pending = list_pending_recommendations()
        pending_ids = {r["id"] for r in pending}

        assert rec_id_pending in pending_ids, "Pending rec should be in list"
        assert rec_id_to_adopt not in pending_ids, "Adopted rec should NOT be in list"

    finally:
        _cleanup(pg_conn, test_strategy_id, [rec_id_pending, rec_id_to_adopt])
