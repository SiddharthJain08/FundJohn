"""SP-1 follow-up: data_provider_health writer.

Records per-(provider, endpoint, hour) success/error counts for the dashboard
Data Health tile (src/channels/dashboard/server.js:/api/data-health).

Usage:
    from src.maintenance.provider_health import record

    try:
        do_alpaca_call()
        record('alpaca', 'options_chain', success=True)
    except Exception as e:
        record('alpaca', 'options_chain', success=False, error=str(e))
        raise

The helper is best-effort — DB failures are swallowed so provider-health
recording never blocks the actual data fetch it's instrumenting.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


def record(provider: str, endpoint: str, *, success: bool, error: Optional[str] = None) -> None:
    """Upsert one provider call into data_provider_health, bucketed by hour.

    Args:
        provider:  short provider tag, e.g. 'alpaca', 'fmp', 'yfinance', 'edgar'.
        endpoint:  short endpoint tag within the provider, e.g. 'options_chain',
                   'news', 'fundamentals'. Combine with provider for the table PK.
        success:   True when the call returned a usable response, False for any
                   error (network, 4xx, 5xx, parse failure, timeout, etc).
        error:     short error message (max ~200 chars) when success=False.
                   Captured into last_error for operator triage.
    """
    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        return
    try:
        import psycopg2  # lazy — keeps the helper importable in test contexts
        success_inc = 1 if success else 0
        error_inc = 0 if success else 1
        err_msg = (error or None) if not success else None
        # NOW() is captured server-side for last_error_at; window_start is
        # truncated to the hour for natural rollup buckets.
        with psycopg2.connect(pg_uri) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_provider_health
                    (provider, endpoint, success_count, error_count,
                     last_error, last_error_at, window_start)
                VALUES (%s, %s, %s, %s, %s,
                        CASE WHEN %s IS NULL THEN NULL ELSE NOW() END,
                        date_trunc('hour', NOW()))
                ON CONFLICT (provider, endpoint, window_start) DO UPDATE SET
                    success_count = data_provider_health.success_count
                                  + EXCLUDED.success_count,
                    error_count   = data_provider_health.error_count
                                  + EXCLUDED.error_count,
                    last_error    = COALESCE(EXCLUDED.last_error,
                                             data_provider_health.last_error),
                    last_error_at = COALESCE(EXCLUDED.last_error_at,
                                             data_provider_health.last_error_at)
                """,
                (provider, endpoint, success_inc, error_inc,
                 (err_msg[:200] if err_msg else None),
                 err_msg),
            )
            conn.commit()
    except Exception as e:
        # Never let provider-health recording break the actual fetch.
        log.debug('provider_health.record swallowed: %s', e)
