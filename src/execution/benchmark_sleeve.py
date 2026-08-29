"""benchmark_sleeve.py — which strategies are benchmark (beta) sleeves.

Spec: docs/specs/2026-08-29-benchmark-relative-sizing-spec.md §2.4.
Source of truth at runtime is strategy_registry.parameters ->> 'benchmark_sleeve'
(mirrored from the strategy class attribute BaseStrategy.benchmark_sleeve at
registration). The sizer never imports strategy classes, so the registry is
the only place it can read the flag from.

Fail-open contract: a DB failure returns an EMPTY set (logged). Consequence for
that cycle: no ticker is treated as a benchmark ticker, so the beta sleeve is
subject to the acting gate / hurdle / caps like any alpha ticker — a
conservative failure (less beta), never an unbounded one.
"""
from __future__ import annotations
import logging
import os

logger = logging.getLogger(__name__)

PARAM_KEY = 'benchmark_sleeve'


def load_benchmark_sleeve_ids(conn=None) -> set[str]:
    """Strategy ids whose registry parameters carry benchmark_sleeve=true."""
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM strategy_registry WHERE (parameters ->> %s) = 'true'",
                        (PARAM_KEY,))
            return {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning('[bench_sleeve] registry read failed (%s); no benchmark sleeves this cycle', e)
        return set()
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def benchmark_tickers(ticker_meta: dict, bench_ids: set[str], net_sign: dict | None = None) -> set[str]:
    """Tickers with at least one benchmark-sleeve contributor: any direction
    unless net_sign is given; then only a contributor acting in the ticker's
    net direction counts, and a net-sign-0 ticker never qualifies.
    ticker_meta is the sizer's {ticker: {'strategies': [...], 'directions': [...]}}."""
    if not bench_ids:
        return set()
    if net_sign is None:
        return {t for t, m in ticker_meta.items()
                if any(s in bench_ids for s in (m or {}).get('strategies', []))}
    out = set()
    for t, m in ticker_meta.items():
        sgn = net_sign.get(t, 0)
        if sgn == 0:
            continue
        m = m or {}
        if any(s in bench_ids and int(d) == sgn
               for s, d in zip(m.get('strategies', []), m.get('directions', []))):
            out.add(t)
    return out
