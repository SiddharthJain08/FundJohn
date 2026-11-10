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


def load_benchmark_sleeve_params(conn=None) -> dict[str, dict]:
    """{strategy_id: registry parameters} for every benchmark sleeve (crypto
    engine Phase 3). Sibling of load_benchmark_sleeve_ids (whose set contract
    is unchanged); callers read `instrument_class` (default 'equity') and
    `benchmark_ticker` (default 'SPY') from it via sleeve_benchmark().
    Same fail-open contract: a DB failure returns {} (logged)."""
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cur.execute("SELECT id, parameters FROM strategy_registry WHERE (parameters ->> %s) = 'true'",
                        (PARAM_KEY,))
            out: dict[str, dict] = {}
            for r in cur.fetchall():
                p = r[1]
                if isinstance(p, str):
                    import json
                    try:
                        p = json.loads(p)
                    except Exception:
                        p = {}
                out[r[0]] = dict(p) if isinstance(p, dict) else {}
            return out
    except Exception as e:
        logger.warning('[bench_sleeve] registry params read failed (%s); sleeves default to SPY/equity', e)
        return {}
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def sleeve_benchmark(params: dict | None) -> tuple[str, str]:
    """(benchmark_ticker, instrument_class) for one sleeve's registry
    parameters. Absent -> ('SPY', 'equity'); a crypto sleeve without an
    explicit ticker -> 'BTC-USD'; a ticker without an explicit class infers
    it from the ticker's regime partition (BTC-USD -> 'crypto', SPY ->
    'equity'), so the S_m partition and the crypto floor_zero can never
    disagree. An explicit class always wins."""
    p = params or {}
    tkr = p.get('benchmark_ticker')
    raw_cls = p.get('instrument_class')
    if raw_cls:
        cls = str(raw_cls).strip().lower()
    elif tkr:
        from execution.benchmark_sizing import benchmark_regime_source
        cls = 'crypto' if benchmark_regime_source(str(tkr)) == 'crypto' else 'equity'
    else:
        cls = 'equity'
    if not tkr:
        tkr = 'BTC-USD' if cls == 'crypto' else 'SPY'
    return str(tkr), cls


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
