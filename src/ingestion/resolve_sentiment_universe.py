"""src/ingestion/resolve_sentiment_universe.py — runtime universe assembly
for the sentiment ingestion step.

3 sources, expands automatically as universe_config, open positions, or
recent signals grow. No hardcoded ticker lists.

Sources (after 2026-05-20 schema audit):
  1. `universe_config WHERE active = TRUE` — canonical active ticker roster.
  2. Open positions — `SELECT DISTINCT es.ticker FROM signal_pnl sp
     JOIN execution_signals es ON sp.signal_id = es.id WHERE sp.closed_at IS NULL`.
  3. Recent-week signals — `SELECT DISTINCT ticker FROM execution_signals
     WHERE created_at > NOW() - INTERVAL '7 days'` (proxy for tickers
     active strategies currently care about).

Raises on DB error — sentiment without a universe is meaningless and
silently scoring an empty set would be worse than a hard fail.
"""
from __future__ import annotations
import os
from typing import List

import psycopg2


_UNIVERSE_CONFIG_QUERY = """
    SELECT DISTINCT ticker FROM universe_config
     WHERE active = TRUE
"""

_OPEN_POSITIONS_QUERY = """
    SELECT DISTINCT es.ticker
      FROM signal_pnl sp
      JOIN execution_signals es ON sp.signal_id = es.id
     WHERE sp.closed_at IS NULL
"""

_RECENT_SIGNALS_QUERY = """
    SELECT DISTINCT ticker FROM execution_signals
     WHERE created_at > NOW() - INTERVAL '7 days'
"""


def current_universe(postgres_uri: str | None = None) -> List[str]:
    """Return the sorted, deduplicated, uppercased union of tickers from 3 sources.

    Args:
        postgres_uri: optional Postgres URI; falls back to ``$POSTGRES_URI``.

    Returns:
        Sorted list of uppercase ticker symbols. Empty list only if all 3
        queries returned zero rows (highly unlikely in production).

    Raises:
        KeyError: if no URI is supplied and ``POSTGRES_URI`` is unset.
        Exception: any DB connection / query error propagates. Sentiment
            without a universe is meaningless — fail loudly.
    """
    uri = postgres_uri or os.environ['POSTGRES_URI']
    seen: set[str] = set()
    queries = (
        _UNIVERSE_CONFIG_QUERY,
        _OPEN_POSITIONS_QUERY,
        _RECENT_SIGNALS_QUERY,
    )
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            for sql in queries:
                cur.execute(sql)
                for (ticker,) in cur.fetchall():
                    if ticker:
                        # Defensive .upper() — DB columns are mixed-case in
                        # legacy rows; downstream code keys on uppercase.
                        seen.add(ticker.upper())
    return sorted(seen)


if __name__ == '__main__':
    import json
    print(json.dumps(current_universe(), indent=2))
