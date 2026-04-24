#!/usr/bin/env python3
"""fingerprint_dedup.py — check a proposed strategy against live + staged work.

Invoked by MasterMindJohn inside a campaign loop, BEFORE inserting a new
research_candidates / strategy_staging row. Two checks:

1. **Exact-slug collision**: `name` already exists in strategy_registry or
   strategy_staging (pending|approved|promoted) → hard duplicate.

2. **Fingerprint match**: formula_tokens Jaccard ≥ 0.6 AND overlapping regime
   set with any existing strategy_signatures.json entry → soft duplicate.

Invocation:

  python3 src/research/fingerprint_dedup.py \\
      --slug momentum_12_1 \\
      --tokens momentum,rv,decile \\
      --regimes any

Output (JSON stdout):

  {
    "duplicate": true,
    "reason": "exact_slug_in_registry",
    "matches": [
      {"source":"strategy_registry","id":"momentum_12_1","match_type":"exact_slug","jaccard":1.0}
    ]
  }

Exit code 0 always (even for duplicates — caller decides what to do).
Exit code 1 only on hard errors (DB down, etc.).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

PG_URI = os.environ.get(
    "POSTGRES_URI", "postgresql://openclaw:password@localhost:5432/openclaw"
)
SIGNATURES_PATH = os.environ.get(
    "OPENCLAW_STRATEGY_SIGNATURES",
    "/root/openclaw/src/strategies/strategy_signatures.json",
)
CANONICAL_SIGNATURES_PATH = os.environ.get(
    "OPENCLAW_CANONICAL_SIGNATURES",
    "/root/openclaw/src/research/canonical_signatures.json",
)
JACCARD_THRESHOLD = 0.60


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def _load_signatures() -> dict:
    """Load fingerprints of *already-implemented* strategies (StrategyCoder
    writes these when producing implementations/*.py).

    canonical_signatures.json is deliberately NOT merged here — that file is a
    lookup catalogue used elsewhere (persona reference for MMJ). Using it for
    dedup would incorrectly collide canon variants (momentum_6_1 vs 12_1)
    against each other inside the same campaign.
    """
    try:
        with open(SIGNATURES_PATH) as fh:
            return json.load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        print(f"[dedup] warning: {SIGNATURES_PATH}: {exc}", file=sys.stderr)
        return {}


def _load_staging_fingerprints() -> list[dict]:
    """Load staged strategies whose hunter_result_json (from linked candidate)
    has a similarity_fingerprint we can compare against.
    """
    with psycopg2.connect(PG_URI, connect_timeout=5) as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT s.id::text AS id, s.name,
                      rc.hunter_result_json->'similarity_fingerprint' AS fp,
                      rc.hunter_result_json->'regime_applicability' AS regimes
                 FROM strategy_staging s
                 LEFT JOIN research_candidates rc
                   ON rc.candidate_id::text = s.source_paper_id
                WHERE s.status IN ('pending','approved','promoted')
                  AND rc.hunter_result_json IS NOT NULL"""
        )
        return [dict(r) for r in cur.fetchall()]


def _fetch_existing_names() -> dict[str, list[str]]:
    """Return {registry: [ids], staging: [names]} — case-insensitive."""
    with psycopg2.connect(PG_URI, connect_timeout=5) as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, name FROM strategy_registry WHERE COALESCE(deprecated_at, 'epoch'::timestamptz) < NOW() - INTERVAL '999 years' OR deprecated_at IS NULL")
        reg = [(r["id"], r["name"]) for r in cur.fetchall()]
        cur.execute(
            "SELECT id::text AS id, name FROM strategy_staging "
            "WHERE status IN ('pending','approved','promoted')"
        )
        stg = [(r["id"], r["name"]) for r in cur.fetchall()]
    return {"registry": reg, "staging": stg}


def check_duplicate(slug: str, tokens: list[str], regimes: list[str]) -> dict:
    token_set  = {t.strip().lower() for t in tokens if t.strip()}
    regime_set = {r.strip().upper() for r in regimes if r.strip()}
    matches: list[dict[str, Any]] = []

    # 1. Exact-slug check against strategy_registry + strategy_staging.
    try:
        existing = _fetch_existing_names()
    except Exception as exc:  # noqa: BLE001
        return {"duplicate": False, "error": f"db: {type(exc).__name__}: {exc}", "matches": []}

    slug_lower = slug.lower()
    for sid, sname in existing["registry"]:
        if sid.lower() == slug_lower or (sname or "").lower() == slug_lower:
            matches.append({
                "source":     "strategy_registry",
                "id":         sid,
                "name":       sname,
                "match_type": "exact_slug",
                "jaccard":    1.0,
            })
    for sid, sname in existing["staging"]:
        if (sname or "").lower() == slug_lower:
            matches.append({
                "source":     "strategy_staging",
                "id":         sid,
                "name":       sname,
                "match_type": "exact_slug",
                "jaccard":    1.0,
            })

    # 2. Fingerprint Jaccard against strategy_signatures.json (implemented
    #    strategies only) — prevents re-doing work StrategyCoder already did.
    sigs = _load_signatures()
    for sid, sig in sigs.items():
        if sid.lower() == slug_lower:
            continue
        other_tokens  = {t.lower() for t in (sig.get("formula_tokens") or [])}
        other_regimes = {r.upper() for r in (sig.get("regimes") or [])}
        jac = _jaccard(token_set, other_tokens)
        regime_overlap = bool(regime_set & other_regimes) or (not regime_set and not other_regimes) \
            or "ANY" in regime_set or "ANY" in other_regimes
        if jac >= JACCARD_THRESHOLD and regime_overlap:
            matches.append({
                "source":     "strategy_signatures",
                "id":         sid,
                "match_type": "fingerprint",
                "jaccard":    round(jac, 3),
                "overlap_tokens": sorted(token_set & other_tokens),
            })

    # 3. Fingerprint Jaccard against already-staged strategies (pending / approved).
    try:
        staged = _load_staging_fingerprints()
    except Exception as exc:  # noqa: BLE001
        staged = []
        print(f"[dedup] warning: staged lookup: {exc}", file=sys.stderr)
    for r in staged:
        fp = r.get("fp") or {}
        other_tokens = {str(t).lower() for t in (fp.get("formula_tokens") or [])}
        other_regimes = {str(x).upper() for x in (r.get("regimes") or [])}
        if not other_tokens:
            continue
        jac = _jaccard(token_set, other_tokens)
        regime_overlap = bool(regime_set & other_regimes) or (not regime_set and not other_regimes) \
            or "ANY" in regime_set or "ANY" in other_regimes
        if jac >= JACCARD_THRESHOLD and regime_overlap:
            matches.append({
                "source":     "strategy_staging_fingerprint",
                "id":         r["id"],
                "name":       r["name"],
                "match_type": "fingerprint",
                "jaccard":    round(jac, 3),
                "overlap_tokens": sorted(token_set & other_tokens),
            })

    dup = len(matches) > 0
    reason = None
    if dup:
        first = matches[0]
        reason = (
            "exact_slug_in_registry" if first["source"] == "strategy_registry" and first["match_type"] == "exact_slug"
            else "exact_slug_in_staging" if first["source"] == "strategy_staging"
            else "fingerprint_match"
        )
    return {"duplicate": dup, "reason": reason, "matches": matches, "threshold": JACCARD_THRESHOLD}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--tokens", default="",
                        help="Comma-separated formula_tokens")
    parser.add_argument("--regimes", default="any",
                        help="Comma-separated regime names; 'any' matches all")
    args = parser.parse_args()

    tokens  = [t.strip() for t in args.tokens.split(",") if t.strip()]
    regimes = [r.strip() for r in args.regimes.split(",") if r.strip()]
    result  = check_duplicate(args.slug, tokens, regimes)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
