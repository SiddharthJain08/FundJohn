"""lifecycle_universe_adoption.py — Atomic adoption/revert/list of universe recommendations.

PUBLIC API
----------
  adopt_universe_recommendation(rec_id, *, actor="system", pg_uri=None)
  revert_universe_recommendation(strategy_id=None, do_all=False, *, actor="system", pg_uri=None)
  list_pending_recommendations(*, pg_uri=None) -> list[dict]

ATOMICITY + RECOVERY
--------------------
Adopt uses a two-phase write:

  1. Write manifest.tmp  + fsync
  2. BEGIN a DB transaction
     a. UPDATE strategy_universe_recommendations SET approved=TRUE, ... WHERE id=rec_id AND approved IS NULL
        (rowcount == 0 → already decided, raise; no audit row written)
     b. INSERT INTO lifecycle_audit_log ...
  3. os.rename(manifest.tmp, manifest)  ← atomic at filesystem level
  4. conn.commit()

Failure scenarios:

- Crash before step 3 (rename): rename never happened, conn.rollback() called in finally block.
  approved stays NULL. Re-adopt is safe and idempotent.

- Crash between step 3 and step 4 (manifest renamed, commit not yet sent):
  The manifest already holds the new ref. approved is still NULL in DB.
  Re-adopt writes the same ref again (idempotent) and commits, making the DB consistent.

- Step 4 succeeds: approved IS NULL guard refuses any further adopt on this rec_id.

Revert reads the most recent 'universe_filter_adopted' audit row for the strategy_id,
extracts before_state, and writes it back. A new 'universe_filter_reverted' audit row
is appended (never deletes).

IMPORTANT: only the strategies[sid].metadata.universe_filter_ref field is touched.
The full manifest JSON is json.load → mutate the one nested field → json.dump.
No dataclass round-trip (which silently strips unrecognised top-level manifest fields).
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]

# Override via OPENCLAW_MANIFEST_PATH for tests so the real manifest is never mutated.
MANIFEST = Path(os.environ.get("OPENCLAW_MANIFEST_PATH", ROOT / "src" / "strategies" / "manifest.json"))

# The module prefix used by universe_default predicates (must match importlib.import_module path).
PREDICATE_MODULE = "src.strategies.universe_default"


def _pg_connect(pg_uri: str | None = None):
    uri = pg_uri or os.environ.get("POSTGRES_URI")
    if not uri:
        raise RuntimeError("POSTGRES_URI not set and pg_uri not provided")
    return psycopg2.connect(uri)


def _manifest_path() -> Path:
    return Path(os.environ.get("OPENCLAW_MANIFEST_PATH", str(ROOT / "src" / "strategies" / "manifest.json")))


def _ref_for_candidate(candidate: str) -> str:
    """Return the module:attr import ref for a bare candidate name.

    E.g. "large_cap" -> "src.strategies.universe_default:large_cap"
    """
    return f"{PREDICATE_MODULE}:{candidate}"


def _read_manifest(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _write_manifest_atomic(manifest: dict, path: Path) -> None:
    """Write manifest to a .tmp sibling, fsync, then rename into place."""
    tmp_path = path.with_suffix(".tmp")
    content = json.dumps(manifest, indent=2, ensure_ascii=False)
    with open(tmp_path, "w") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.rename(tmp_path, path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def adopt_universe_recommendation(
    rec_id: int,
    *,
    actor: str = "system",
    pg_uri: str | None = None,
) -> dict:
    """Atomically adopt a pending universe recommendation.

    Steps:
      1. Fetch the rec row; validate approved IS NULL.
      2. Build the new universe_filter_ref string from candidate_predicate.
      3. Write manifest.tmp with the updated ref + fsync.
      4. In a single DB transaction: UPDATE rec row + INSERT audit row.
      5. os.rename(manifest.tmp, manifest).
      6. conn.commit().

    Returns a dict with the adopted strategy_id and new ref string.

    Raises:
      ValueError  — rec not found or already decided (approved IS NOT NULL).
      RuntimeError — POSTGRES_URI not set.
    """
    conn = _pg_connect(pg_uri)
    manifest_path = _manifest_path()
    tmp_path = manifest_path.with_suffix(".tmp")

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Fetch rec; must exist and be pending (approved IS NULL).
            cur.execute(
                "SELECT * FROM strategy_universe_recommendations WHERE id = %s",
                (rec_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"Recommendation {rec_id} not found")
            if row["approved"] is not None:
                raise ValueError(
                    f"Recommendation {rec_id} for strategy {row['strategy_id']!r} "
                    f"is already decided (approved={row['approved']})"
                )

            strategy_id = row["strategy_id"]
            candidate = row["candidate_predicate"]  # bare name, e.g. "large_cap"
            new_ref = _ref_for_candidate(candidate)

            # Read current manifest and capture before_state.
            manifest = _read_manifest(manifest_path)
            strategy_entry = manifest.get("strategies", {}).get(strategy_id)
            if strategy_entry is None:
                raise ValueError(f"Strategy {strategy_id!r} not found in manifest")

            before_state = strategy_entry.get("metadata", {}).get("universe_filter_ref")

            # Write the updated ref into the manifest (only touch this one field).
            strategy_entry.setdefault("metadata", {})["universe_filter_ref"] = new_ref
            manifest["strategies"][strategy_id] = strategy_entry

            # --- Two-phase write begins ---

            # Phase 1: write tmp file + fsync (before DB changes).
            content = json.dumps(manifest, indent=2, ensure_ascii=False)
            with open(tmp_path, "w") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())

            # Phase 2: DB transaction — UPDATE rec + INSERT audit.
            cur.execute(
                """
                UPDATE strategy_universe_recommendations
                SET approved = TRUE,
                    approved_at = NOW(),
                    approved_by = %s,
                    adopted = TRUE,
                    adopted_at = NOW()
                WHERE id = %s AND approved IS NULL
                """,
                (actor, rec_id),
            )
            if cur.rowcount != 1:
                # Already decided between our SELECT and UPDATE — abort.
                raise ValueError(
                    f"Recommendation {rec_id} was decided concurrently; "
                    "no changes written"
                )

            cur.execute(
                """
                INSERT INTO lifecycle_audit_log
                  (event, strategy_id, before_state, after_state, actor)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    "universe_filter_adopted",
                    strategy_id,
                    before_state,   # may be None (strategy had no ref yet)
                    new_ref,
                    actor,
                ),
            )
            audit_id = cur.fetchone()["id"]

            # Phase 3: atomic rename — manifest is now live on disk.
            os.rename(tmp_path, manifest_path)

            # Phase 4: commit — DB now consistent with disk.
            conn.commit()

    except Exception:
        conn.rollback()
        # Clean up tmp file if it exists.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    finally:
        conn.close()

    return {
        "rec_id": rec_id,
        "strategy_id": strategy_id,
        "before_ref": before_state,
        "after_ref": new_ref,
        "audit_id": audit_id,
    }


def revert_universe_recommendation(
    strategy_id: str | None = None,
    do_all: bool = False,
    *,
    actor: str = "system",
    pg_uri: str | None = None,
) -> list[dict]:
    """Revert the most recent universe adoption for one or all strategies.

    Reads the latest 'universe_filter_adopted' audit row to get before_state,
    restores it in the manifest, and writes a 'universe_filter_reverted' audit row.

    If before_state is None/empty (strategy had no prior ref), the
    universe_filter_ref key is removed from the manifest metadata — writing an
    empty string would crash the resolver's rsplit(":").

    Args:
      strategy_id: revert this specific strategy (mutually exclusive with do_all).
      do_all:      revert ALL strategies that have an adoption audit row.

    Returns a list of result dicts (one per reverted strategy).

    Raises:
      ValueError — neither strategy_id nor do_all supplied, or strategy_id
                   given with do_all=True, or no adopt audit row found for a
                   single-strategy revert.
    """
    if do_all and strategy_id is not None:
        raise ValueError("Provide either strategy_id or do_all=True, not both")
    if not do_all and strategy_id is None:
        raise ValueError("Provide strategy_id or do_all=True")

    conn = _pg_connect(pg_uri)
    manifest_path = _manifest_path()
    tmp_path = manifest_path.with_suffix(".tmp")
    results: list[dict] = []

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Determine strategy_ids to revert.
            if do_all:
                cur.execute(
                    """
                    SELECT DISTINCT ON (strategy_id) strategy_id, before_state, after_state, id AS audit_id
                    FROM lifecycle_audit_log
                    WHERE event = 'universe_filter_adopted'
                    ORDER BY strategy_id, occurred_at DESC
                    """
                )
                adopt_rows = list(cur.fetchall())
                if not adopt_rows:
                    return []
            else:
                cur.execute(
                    """
                    SELECT strategy_id, before_state, after_state, id AS audit_id
                    FROM lifecycle_audit_log
                    WHERE event = 'universe_filter_adopted' AND strategy_id = %s
                    ORDER BY occurred_at DESC
                    LIMIT 1
                    """,
                    (strategy_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError(
                        f"No adoption audit row found for strategy {strategy_id!r}; "
                        "nothing to revert"
                    )
                adopt_rows = [row]

            # Load manifest once; apply all reversions to the in-memory dict.
            manifest = _read_manifest(manifest_path)

            for adopt_row in adopt_rows:
                sid = adopt_row["strategy_id"]
                before_ref = adopt_row["before_state"]  # may be None
                current_ref = adopt_row["after_state"]

                strategy_entry = manifest.get("strategies", {}).get(sid)
                if strategy_entry is None:
                    # Strategy not in manifest — skip (do_all) or raise (single).
                    if do_all:
                        continue
                    raise ValueError(f"Strategy {sid!r} not found in manifest")

                # Restore or remove the ref.
                meta = strategy_entry.setdefault("metadata", {})
                if before_ref:
                    meta["universe_filter_ref"] = before_ref
                else:
                    meta.pop("universe_filter_ref", None)
                manifest["strategies"][sid] = strategy_entry

                results.append({
                    "strategy_id": sid,
                    "before_ref": current_ref,   # what we're reverting FROM
                    "after_ref": before_ref,      # what we're restoring TO
                })

            if not results:
                return []

            # Write updated manifest to tmp + fsync.
            content = json.dumps(manifest, indent=2, ensure_ascii=False)
            with open(tmp_path, "w") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())

            # Insert audit rows in one transaction.
            for r in results:
                cur.execute(
                    """
                    INSERT INTO lifecycle_audit_log
                      (event, strategy_id, before_state, after_state, actor)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        "universe_filter_reverted",
                        r["strategy_id"],
                        r["before_ref"],
                        r["after_ref"],
                        actor,
                    ),
                )
                r["audit_id"] = cur.fetchone()["id"]

            # Atomic rename then commit.
            os.rename(tmp_path, manifest_path)
            conn.commit()

    except Exception:
        conn.rollback()
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    finally:
        conn.close()

    return results


def list_pending_recommendations(*, pg_uri: str | None = None) -> list[dict]:
    """Return all strategy_universe_recommendations rows where approved IS NULL."""
    conn = _pg_connect(pg_uri)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, strategy_id, recommended_at, current_predicate,
                       candidate_predicate, candidate_set_id, backtest_summary,
                       rationale, mastermind_cost_usd
                FROM strategy_universe_recommendations
                WHERE approved IS NULL
                ORDER BY recommended_at DESC
                """
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lifecycle_universe_adoption",
        description="Adopt / revert / list universe recommendations",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # adopt
    adopt_p = sub.add_parser("adopt", help="Adopt a pending recommendation by rec_id")
    adopt_p.add_argument("--rec-id", type=int, required=True, help="Row ID in strategy_universe_recommendations")
    adopt_p.add_argument("--actor", default="operator", help="Actor label for the audit log")

    # revert
    revert_p = sub.add_parser("revert", help="Revert the most recent universe adoption")
    revert_exc = revert_p.add_mutually_exclusive_group(required=True)
    revert_exc.add_argument("--strategy-id", help="Revert a specific strategy")
    revert_exc.add_argument("--all", dest="do_all", action="store_true", help="Revert all strategies")
    revert_p.add_argument("--actor", default="operator", help="Actor label for the audit log")

    # list
    sub.add_parser("list", help="List pending (undecided) recommendations")

    args = parser.parse_args(argv)

    if args.command == "adopt":
        result = adopt_universe_recommendation(args.rec_id, actor=args.actor)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "revert":
        results = revert_universe_recommendation(
            strategy_id=args.strategy_id,
            do_all=args.do_all,
            actor=args.actor,
        )
        print(json.dumps(results, indent=2, default=str))

    elif args.command == "list":
        rows = list_pending_recommendations()
        print(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
