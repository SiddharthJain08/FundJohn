"""SP-7 Phase C — per-strategy LIVE universes (C1) + shadow-parity writer.

Mirror-clamp semantics (operator decision D3, spec §3.3): a strategy's
predicate decides CLAMPABLE EQUITIES only; every non-equity ticker in the
engine's fallback universe (etf / index / crypto / absent-from-metadata)
passes through to every strategy. Un-adopted (sp500-default) strategies
therefore reproduce today's clamped universe BY CONSTRUCTION, and the two
live non-equity strategies keep their tickers under any predicate.

Per-strategy universe is always ⊆ the fallback universe (parquet tickers),
so the price panel always has every column a strategy may reference.

Fail-open per strategy: any resolve error leaves that strategy on the FULL
fallback universe (never empty a live universe) and records the error.

The classification helpers are LIFTED from src/execution/universe_clamp.py
(not imported): the clamp is DELETED at the end of C1 (spec §3.6) and this
module must survive that deletion.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

logger = logging.getLogger("ENGINE")

MANIFEST_PATH = "/root/openclaw/src/strategies/manifest.json"


def _default_meta_fetch() -> dict[str, tuple[str, bool]]:
    """{symbol: (asset_class, in_sp500)} from the latest metadata snapshot."""
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, asset_class, in_sp500
                  FROM ticker_metadata_snapshots
                 WHERE snapshot_date = (
                       SELECT max(snapshot_date) FROM ticker_metadata_snapshots)
            """)
            return {s: (ac, bool(sp)) for s, ac, sp in cur.fetchall()}
    finally:
        conn.close()


def _default_category_fetch() -> dict[str, str]:
    """{ticker: category} from universe_config (etf/index/crypto/... overlay)."""
    import psycopg2
    conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, category FROM universe_config")
            return dict(cur.fetchall())
    finally:
        conn.close()


def _manifest_universe_refs() -> dict[str, str | None]:
    """universe_filter_ref is NESTED under metadata (the adoption writer at
    lifecycle_universe_adoption.py:176 sets strategies[sid].metadata.
    universe_filter_ref, and _load_predicate at universe_resolver.py:38-39
    reads the same path). Reading it top-level would mislabel every adopted
    strategy as un-adopted → permanent parity WARN → flip blocked."""
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    return {sid: rec.get("metadata", {}).get("universe_filter_ref")
            for sid, rec in manifest.get("strategies", {}).items()}


def _predicate_name(ref: str | None) -> str:
    return ref.rsplit(":", 1)[1] if ref else "sp500"


def build_resolver(conn=None):
    """Production resolver on the Phase C fast adapters: memoized metadata
    fetch (one DB query per as_of) + CoverageIndex (one parquet read)."""
    from src.strategies._db_adapters import PostgresMetadataDB
    from src.strategies.coverage_index import CoverageIndex
    from src.strategies.universe_resolver import UniverseResolver

    def manifest_loader():
        with open(MANIFEST_PATH) as f:
            return json.load(f)

    return UniverseResolver(
        db=PostgresMetadataDB(os.environ["POSTGRES_URI"], conn=conn),
        coverage=CoverageIndex.from_parquet(
            "/root/openclaw/data/master/prices.parquet"),
        manifest_loader=manifest_loader,
    )


def build_strategy_universes(strategy_ids, as_of, fallback_universe,
                             resolver=None,
                             meta_fetch=_default_meta_fetch,
                             category_fetch=_default_category_fetch):
    """{strategy_id: {'universe': [...], 'predicate': str, 'adopted': bool,
                      'error': str | None}}"""
    refs = _manifest_universe_refs()
    if resolver is None:
        resolver = build_resolver()
    meta = meta_fetch()
    categories = category_fetch()

    def is_clampable_equity(sym: str) -> bool:
        # Symbol-form bridge (SP-7 §11 / ab4238f): parquet/universe_config use
        # dash form ('BRK-B'); ticker_metadata_snapshots uses Alpaca dot form.
        meta_sym = sym if sym in meta else sym.replace("-", ".")
        in_meta = meta_sym in meta
        category = categories.get(sym, "equity" if in_meta else None)
        return in_meta and meta[meta_sym][0] == "us_equity" and category == "equity"

    clampable = {s for s in fallback_universe if is_clampable_equity(s)}
    passthrough = [s for s in fallback_universe if s not in clampable]

    out = {}
    for sid in strategy_ids:
        if sid not in refs:
            logger.warning("[live-universe] %s missing from manifest — default sp500", sid)
        ref = refs.get(sid)
        pred_name = _predicate_name(ref)
        adopted = pred_name != "sp500"
        try:
            resolved = set(resolver.resolve(sid, as_of))
            kept_equities = [s for s in fallback_universe
                             if s in clampable
                             and (s in resolved or s.replace("-", ".") in resolved)]
            universe = sorted(set(kept_equities) | set(passthrough))
            out[sid] = {"universe": universe, "predicate": pred_name,
                        "adopted": adopted, "error": None}
        except Exception as e:  # noqa: BLE001 — never empty a live universe
            logger.error("[live-universe] %s resolve failed — fail-open to "
                         "shared universe: %s", sid, e)
            out[sid] = {"universe": list(fallback_universe), "predicate": pred_name,
                        "adopted": adopted, "error": str(e)}
    return out


def write_shadow_parity(run_date, strategy_ids, actual_universe,
                        conn=None, resolver=None,
                        meta_fetch=_default_meta_fetch,
                        category_fetch=_default_category_fetch):
    """Diff resolver-built per-strategy universes against the actual clamped
    universe; UPSERT into universe_shadow_parity (migration 133).

    Opens its OWN connection by default — the engine's transaction must not
    be committed mid-flight by a sidecar. Caller wraps in try/except; any
    exception here is non-fatal to the signals step.
    """
    import psycopg2
    as_of = run_date if isinstance(run_date, date) else date.fromisoformat(str(run_date))
    built = build_strategy_universes(strategy_ids, as_of, list(actual_universe),
                                     resolver=resolver, meta_fetch=meta_fetch,
                                     category_fetch=category_fetch)
    actual = set(actual_universe)
    own_conn = conn is None
    if own_conn:
        conn = psycopg2.connect(os.environ["POSTGRES_URI"])
    try:
        with conn.cursor() as cur:
            for sid, info in built.items():
                resolved = set(info["universe"])
                added = sorted(resolved - actual)
                removed = sorted(actual - resolved)
                cur.execute("""
                    INSERT INTO universe_shadow_parity
                        (run_date, strategy_id, predicate, n_resolved, n_actual,
                         added_tickers, removed_tickers, is_adopted, resolve_error)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
                    ON CONFLICT (run_date, strategy_id) DO UPDATE SET
                        predicate       = EXCLUDED.predicate,
                        n_resolved      = EXCLUDED.n_resolved,
                        n_actual        = EXCLUDED.n_actual,
                        added_tickers   = EXCLUDED.added_tickers,
                        removed_tickers = EXCLUDED.removed_tickers,
                        is_adopted      = EXCLUDED.is_adopted,
                        resolve_error   = EXCLUDED.resolve_error
                """, (as_of, sid, info["predicate"], len(resolved), len(actual),
                      json.dumps(added), json.dumps(removed),
                      info["adopted"], info["error"]))
        conn.commit()
        n_drift = sum(1 for s, i in built.items()
                      if not i["adopted"] and set(i["universe"]) != actual)
        logger.info("[live-universe] shadow parity: %d strategies, %d un-adopted drift",
                    len(built), n_drift)
    finally:
        if own_conn:
            conn.close()
