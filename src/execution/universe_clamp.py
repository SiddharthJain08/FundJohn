"""SP-7 Phase A4 — gated engine universe clamp.

The live signals engine's fallback universe is ALL tickers in prices.parquet
(engine.py fallback — log-verified 2026-06-04). Any broad price backfill is
therefore a live-universe change. This clamp pins the engine's EQUITY universe
to a named predicate while data widens underneath; non-equity tickers
(benchmarks, sector ETFs, indices, crypto) always pass through.

Gate: OPENCLAW_ENGINE_UNIVERSE_CLAMP (only supported value: 'sp500';
unset → identity; any other value → warn + fail open). Retired (deleted, not
gated-off) by SP-7 Phase C when the resolver takes over live signals.

Fail-open by design: a broken clamp or DB error must never empty the live
universe (mirrors regime-gate fail-open conventions).

Supported predicates (Phase A only):
  sp500   — keep only us_equity/equity tickers that have in_sp500=True in the
             latest ticker_metadata_snapshots; all non-equity categories (etf,
             index, crypto, screener_candidate, etc.) and any ticker absent from
             metadata pass through untouched.

  anything else → warning + fail open (returns universe unchanged)
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("ENGINE")


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


def clamp_universe(
    universe: list[str],
    meta_fetch=_default_meta_fetch,
    category_fetch=_default_category_fetch,
) -> list[str]:
    """Return the clamped universe, or the original list unchanged on any error.

    Args:
        universe:       Ordered list of ticker strings from the engine fallback.
        meta_fetch:     Callable returning {symbol: (asset_class, in_sp500)}.
                        Defaults to a live DB query against ticker_metadata_snapshots.
        category_fetch: Callable returning {ticker: category}.
                        Defaults to a live DB query against universe_config.

    Returns:
        Filtered list (same type/order as input) when gate is 'sp500';
        original list unchanged when gate is unset or any error occurs.
    """
    pred_name = os.environ.get("OPENCLAW_ENGINE_UNIVERSE_CLAMP")
    if not pred_name:
        # Gate unset — identity, zero DB calls
        return universe

    if pred_name != "sp500":
        logger.warning(
            f"universe clamp: unsupported predicate '{pred_name}' — failing open"
        )
        return universe

    try:
        meta = meta_fetch()
        categories = category_fetch()
    except Exception as e:  # noqa: BLE001 — fail-open: never empty the live universe
        logger.warning(f"universe clamp 'sp500' failed open: {e}")
        return universe

    kept: list[str] = []
    dropped = 0
    for sym in universe:
        # Symbol-form bridge (SP-7 §11 acceptance finding): parquet/universe_config
        # store dash form ('ALB-PRA', 'BRK-B') while ticker_metadata_snapshots
        # stores Alpaca's dot form ('ALB.PRA', 'BRK.B'). Fall back to the dot
        # form so preferreds/share classes resolve instead of failing open.
        meta_sym = sym if sym in meta else sym.replace("-", ".")
        in_meta = meta_sym in meta
        # Determine category: explicit from universe_config, else infer.
        # Tickers absent from metadata are non-equity by convention (pass through).
        category = categories.get(sym, "equity" if in_meta else None)
        is_clampable_equity = (
            in_meta
            and meta[meta_sym][0] == "us_equity"
            and category == "equity"
        )
        if not is_clampable_equity:
            # Non-equity (etf, index, crypto, screener_candidate, absent) — pass through
            kept.append(sym)
        elif meta[meta_sym][1]:
            # is_clampable_equity AND in_sp500 — pass through
            kept.append(sym)
        else:
            # is_clampable_equity AND NOT in_sp500 — clamp out
            dropped += 1

    logger.info(
        f"universe clamp 'sp500': kept {len(kept)}, dropped {dropped}"
    )
    return kept
