"""quote_monitor_parity — D2.7 standalone parity-capture entrypoint.

Runs the QuoteMonitor against the same ticker universe as the daily
collect step, then writes pairwise (Polygon-vs-other) divergence rows to
the ``quote_monitor_parity`` table. The operator reviews this table for
5 trading days BEFORE flipping ``OPENCLAW_UNIFIED_QUOTES=1`` in production.

This module is NOT wired into the daily cycle. Invoke from CLI:

    OPENCLAW_UNIFIED_QUOTES=1 PYTHONPATH=src python3 \
        -m src.ingestion.quote_monitor_parity --tickers AAPL,MSFT,SPY

or import-and-call from an ad-hoc operator script.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Iterable, Optional

from src.ingestion.quote_monitor import (
    ENV_GATE,
    FanOutResult,
    QuoteMonitor,
    fetch_quotes,
    gate_enabled,
)


PRIMARY_SOURCE = 'polygon'  # spec line 759 — see commit message + module doc


def _build_default_sources() -> list:
    """Instantiate Polygon + FMP + Alpaca + Yahoo from env credentials.

    Adapters with missing credentials are kept in the list — they'll
    return {} from fetch() and surface as a "no data" source, which is
    informative for the parity-capture operator.
    """
    from src.ingestion.quote_sources.polygon import PolygonQuoteSource
    from src.ingestion.quote_sources.fmp     import FMPQuoteSource
    from src.ingestion.quote_sources.alpaca  import AlpacaQuoteSource
    from src.ingestion.quote_sources.yahoo   import YahooQuoteSource
    return [
        PolygonQuoteSource(),
        FMPQuoteSource(),
        AlpacaQuoteSource(),
        YahooQuoteSource(),
    ]


def compute_parity_rows(
    result: FanOutResult,
    primary_source: str = PRIMARY_SOURCE,
) -> list[dict]:
    """Build the pairwise divergence rows for the parity table.

    For each ticker that the ``primary_source`` resolved, emit one row per
    other source that ALSO resolved the same ticker. If the primary did
    NOT resolve a ticker, we skip it — the parity table is anchored on
    Polygon (spec line 759).
    """
    rows: list[dict] = []
    primary = result.by_source.get(primary_source) or {}
    for ticker, qa in primary.items():
        for src_name, src_quotes in result.by_source.items():
            if src_name == primary_source:
                continue
            qb = src_quotes.get(ticker)
            if qb is None:
                continue
            if qa.price <= 0:
                continue
            div_pct = (qb.price - qa.price) / qa.price * 100.0
            div_bps = int(round((qb.price - qa.price) / qa.price * 1e4))
            rows.append({
                'ticker':         ticker,
                'source_a':       primary_source,
                'price_a':        float(qa.price),
                'source_b':       src_name,
                'price_b':        float(qb.price),
                'divergence_pct': float(div_pct),
                'divergence_bps': div_bps,
                'max_age_sec':    max(qa.age_sec, qb.age_sec),
                'fetched_at_a':   qa.fetched_at,
                'fetched_at_b':   qb.fetched_at,
            })
    return rows


def insert_parity_rows(rows: list[dict], conn=None) -> int:
    """Insert parity rows. ``conn`` is an optional psycopg2 connection
    (tests pass an in-memory fake). Returns rows inserted."""
    if not rows:
        return 0
    if conn is None:
        import psycopg2
        conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        close_after = True
    else:
        close_after = False
    try:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO quote_monitor_parity (
                        ticker, source_a, price_a, source_b, price_b,
                        divergence_pct, divergence_bps, max_age_sec,
                        fetched_at_a, fetched_at_b
                    ) VALUES (
                        %(ticker)s, %(source_a)s, %(price_a)s,
                        %(source_b)s, %(price_b)s,
                        %(divergence_pct)s, %(divergence_bps)s,
                        %(max_age_sec)s, %(fetched_at_a)s, %(fetched_at_b)s
                    )
                    """,
                    r,
                )
        conn.commit()
        return len(rows)
    finally:
        if close_after:
            conn.close()


async def run_parity_capture(
    tickers: Iterable[str],
    sources: Optional[list] = None,
    write_db: bool = True,
) -> tuple[FanOutResult, list[dict], int]:
    """End-to-end: fan out, compute parity rows, optionally insert.

    Returns (fan_out_result, parity_rows, db_rows_inserted)."""
    src_list = sources if sources is not None else _build_default_sources()
    result = await fetch_quotes(tickers, sources=src_list)
    rows = compute_parity_rows(result)
    inserted = insert_parity_rows(rows) if write_db else 0
    return result, rows, inserted


def _cli(argv: list[str]) -> int:
    """python3 -m src.ingestion.quote_monitor_parity entrypoint."""
    p = argparse.ArgumentParser(prog='quote_monitor_parity')
    p.add_argument('--tickers', required=True,
                   help='Comma-separated ticker list, e.g. AAPL,MSFT,SPY')
    p.add_argument('--no-write', action='store_true',
                   help='Skip Postgres insert (dry-run; print rows only).')
    args = p.parse_args(argv)

    if not gate_enabled() and not args.no_write:
        sys.stderr.write(
            f'{ENV_GATE} is not set to 1. Refusing to write to '
            f'quote_monitor_parity without the gate. Re-run with '
            f'--no-write to print rows, or set {ENV_GATE}=1.\n'
        )
        return 2

    tickers = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
    result, rows, inserted = asyncio.run(
        run_parity_capture(tickers, write_db=not args.no_write),
    )
    sys.stdout.write(
        f'sources responded: {sorted(result.by_source.keys())}\n'
        f'sources timed_out: {sorted(result.timed_out)}\n'
        f'sources errored:   {sorted(result.errors.keys())}\n'
        f'winners:           {len(result.winners)}\n'
        f'parity rows:       {len(rows)} '
        f'(inserted={inserted})\n'
    )
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(_cli(sys.argv[1:]))
